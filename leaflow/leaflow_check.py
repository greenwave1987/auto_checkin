import os
import sys
import time
import random
import base64
import socket
import subprocess
import json
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from engine.notify import TelegramNotifier
from engine.main import ConfigReader, SecretUpdater, test_proxy

LOGIN_URL = "https://leaflow.net/login"
DASHBOARD_URL = "https://leaflow.net/dashboard"
BALANCE_URL = "https://leaflow.net/balance"
SCREENSHOT_DIR = "/tmp/leaflow_fail"


# ==================== 工具函数 ====================
def mask_email(email: str):
    if "@" not in email:
        return "***"
    name, domain = email.split("@", 1)
    return f"{name[:2]}***{name[-2:]}@{domain}"


def mask_ip(ip: str):
    if not ip:
        return "***"
    return f"***{ip}"


def mask_password(pwd: str):
    return "*" * 6 + f"({len(pwd)})"


# ==================== 核心类 ====================
class LeaflowTask:
    def __init__(self):
        self.config = ConfigReader()
        self.logs = []
        self.notifier = TelegramNotifier(self.config)
        self.secret = SecretUpdater("LEAFLOW_LOCALS", config_reader=self.config)
        self.gost_proc = None
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)

    # ---------- 日志 ----------
    def log(self, msg, level="INFO"):
        icons = {"INFO": "ℹ️", "SUCCESS": "✅", "ERROR": "❌", "WARN": "⚠️", "STEP": "🔹"}
        line = f"{icons.get(level,'•')} {msg}"
        print(line, flush=True)
        self.logs.append(line)

    # ---------- Gost ----------
    def start_gost_proxy(self, proxy):
        def free_port():
            s = socket.socket()
            s.bind(("", 0))
            port = s.getsockname()[1]
            s.close()
            return port

        port = free_port()
        server = f"http://127.0.0.1:{port}"
        remote = f"socks5://{proxy['username']}:{proxy['password']}@{proxy['server']}:{proxy['port']}"

        self.log(
            f"启动 Gost: ./gost -L :{port} -F ***{proxy['server']}:{proxy['port']}",
            "STEP"
        )

        self.gost_proc = subprocess.Popen(
            ["./gost", "-L", f":{port}", "-F", remote],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        time.sleep(2)
        return {"server": server}

    # ---------- 浏览器 ----------
    def open_browser(self, proxy, storage_b64):
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

        if proxy:
            if proxy.get("type") == "socks5" and proxy.get("username"):
                gost = self.start_gost_proxy(proxy)
                launch_args["proxy"] = {"server": gost["server"]}
                self.log(f"使用 Gost 本地代理: {gost['server']}", "SUCCESS")
            else:
                server = f"{proxy['type']}://{proxy['server']}:{proxy['port']}"
                launch_args["proxy"] = {"server": server}
                self.log(f"启用代理: {mask_ip(proxy['server'])}", "INFO")

        browser = pw.chromium.launch(**launch_args)

        context_args = {
            "viewport": {"width": 1920, "height": 1080},
            "user_agent": "Mozilla/5.0 Chrome/128.0.0.0"
        }

        if storage_b64:
            try:
                decoded = base64.b64decode(storage_b64).decode()
                context_args["storage_state"] = json.loads(decoded)
                self.log("已加载历史 session（base64）", "SUCCESS")
            except Exception as e:
                self.log(f"session 解码失败，忽略并重新登录: {e}", "WARN")

        context = browser.new_context(**context_args)
        page = context.new_page()
        return pw, browser, page

    # ---------- session 校验 ----------
    def check_session_valid(self, page):
        self.log(f"验证 session，有效性检测: {DASHBOARD_URL}", "STEP")
        page.goto(DASHBOARD_URL, timeout=30000)
        page.wait_for_load_state("networkidle")
        if "login" in page.url.lower():
            self.log("session 已失效", "WARN")
            return False
        self.log("session 有效", "SUCCESS")
        return True

    # ---------- 截图 ----------
    def capture_and_notify(self, page, user, reason):
        path = f"{SCREENSHOT_DIR}/{user}_{int(time.time())}.png"
        page.screenshot(path=path, full_page=True)
        self.notifier.send_photo(
            photo_path=path,
            caption=f"❌ Leaflow 登录失败\n账号: {mask_email(user)}\n原因: {reason}"
        )

    # ---------- 登录 ----------
    def login_and_get_storage(self, page, user, pwd):
        self.log(f"打开登录页: {LOGIN_URL}", "STEP")
        page.goto(LOGIN_URL)

        self.log(f"输入账号: {mask_email(user)}", "INFO")
        page.fill("#account", user)

        self.log(f"输入密码: {mask_password(pwd)}", "INFO")
        page.fill("#password", pwd)

        try:
            self.log("点击「保持登录状态」", "STEP")
            page.get_by_role("checkbox", name="保持登录状态").click(force=True)
        except:
            self.log("未找到保持登录复选框", "WARN")

        self.log("点击登录按钮", "STEP")
        page.locator('button[type="submit"]').click()
        page.wait_for_load_state("networkidle", timeout=60000)

        if "login" in page.url.lower():
            raise RuntimeError("登录失败")

        self.log("登录成功，提取 session", "SUCCESS")
        return page.context.storage_state()

    # ---------- 主流程 ----------
    def run(self):
        self.log("Leaflow 多账号任务启动", "STEP")

        accounts = self.config.get_value("LF_INFO") or []
        proxies = self.config.get_value("PROXY_INFO") or []
        lf_locals = self.secret.load() or {}
        new_sessions = {}

        for account, proxy in zip(accounts, proxies):
            user = account["username"]
            pwd = account["password"]

            self.log(f"开始处理账号: {mask_email(user)}", "STEP")
            self.log(f"检测代理: {mask_ip(proxy['server'])}", "STEP")

            if not test_proxy(proxy):
                self.log("代理不可用，回退 wz_proxy", "WARN")
                proxy = self.config.get("wz_proxy")
                test_proxy(proxy)

            pw = browser = None
            try:
                pw, browser, page = self.open_browser(proxy, lf_locals.get(user))

                need_login = True
                if lf_locals.get(user):
                    need_login = not self.check_session_valid(page)

                if need_login:
                    storage = self.login_and_get_storage(page, user, pwd)
                else:
                    storage = page.context.storage_state()

                new_sessions[user] = storage

                self.log(f"打开余额页: {BALANCE_URL}", "STEP")
                page.goto(BALANCE_URL)
                page.wait_for_load_state("networkidle")

                bal = page.locator('p[title]').text_content().strip()
                spent = page.locator('p.text-3xl.font-bold:not([title])').text_content().strip()
                self.log(f"🏦 余额: {bal} | 已消费: {spent}", "INFO")

            except Exception as e:
                self.log(f"{mask_email(user)} 登录异常: {e}", "ERROR")
                self.capture_and_notify(page, user, str(e))

            finally:
                if browser:
                    browser.close()
                if pw:
                    pw.stop()
                if self.gost_proc:
                    self.gost_proc.terminate()
                    self.gost_proc = None

        if new_sessions:
            self.log("📝 准备回写 GitHub Secret", "STEP")
            encoded = {
                k: base64.b64encode(json.dumps(v).encode()).decode()
                for k, v in new_sessions.items()
            }
            self.secret.update(encoded)
            self.log("✅ Secret 回写成功", "SUCCESS")

        self.log("🔔 开始发送通知", "STEP")
        self.notifier.send("Leaflow 自动登录维护", "\n".join(self.logs))


if __name__ == "__main__":
    LeaflowTask().run()
