import os
import sys
import time
import random
import base64
import socket
import subprocess
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ==================== 路径 ====================
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from engine.notify import TelegramNotifier
from engine.main import ConfigReader, SecretUpdater, test_proxy

LOGIN_URL = "https://leaflow.net/login"
DASHBOARD_URL = "https://leaflow.net/dashboard"
BALANCE_URL = "https://leaflow.net/balance"
SCREENSHOT_DIR = "/tmp/leaflow_fail"


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

    # ---------- 启动 Gost ----------
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

        cmd = ["./gost", "-L", f":{port}", "-F", remote]
        self.log(f"启动 Gost: {' '.join(cmd)}", "STEP")

        self.gost_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        time.sleep(2)
        return {"server": server}

    # ---------- 启动浏览器 ----------
    def open_browser(self, proxy=None, storage=None):
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
            try:
                if proxy.get("type") == "socks5" and proxy.get("username"):
                    gost = self.start_gost_proxy(proxy)
                    launch_args["proxy"] = {"server": gost["server"]}
                    self.log(f"使用 Gost 本地代理: {gost['server']}", "SUCCESS")
                else:
                    launch_args["proxy"] = {
                        "server": f"{proxy['type']}://{proxy['server']}:{proxy['port']}"
                    }
                    self.log(f"启用代理: {launch_args['proxy']['server']}", "INFO")
            except Exception as e:
                self.log(f"代理配置失败: {e}", "ERROR")

        browser = pw.chromium.launch(**launch_args)
        context = browser.new_context(
            storage_state=storage,
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        )

        context.add_init_script("""
            Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
            Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});
            Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});
            window.chrome={runtime:{}};
        """)

        return pw, browser, context, context.new_page()

    # ---------- 截图并发送 TG ----------
    def capture_and_notify(self, page, username, reason):
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = f"{SCREENSHOT_DIR}/{username}_{ts}.png"
        try:
            page.screenshot(path=path, full_page=True)
            self.log(f"登录失败截图已保存: {path}", "WARN")
            self.notifier.send_photo(
                photo_path=path,
                caption=f"❌ Leaflow 登录失败\n账号: {username}\n原因: {reason}"
            )
        except Exception as e:
            self.log(f"截图或发送失败: {e}", "ERROR")

    # ---------- session 校验 ----------
    def cookies_ok(self, page):
        page.goto(DASHBOARD_URL, timeout=30000)
        page.wait_for_load_state("networkidle")
        return "login" not in page.url.lower()

    # ---------- 登录 ----------
    def login_and_get_storage(self, page, username, password):
        page.goto(LOGIN_URL)
        page.fill("#account", username)
        page.fill("#password", password)

        try:
            el = page.get_by_role("checkbox", name="保持登录状态").first
            el.wait_for(state="visible", timeout=3000)
            el.click(force=True)
        except PlaywrightTimeoutError:
            pass

        page.locator('button[type="submit"]').click()
        page.wait_for_load_state("networkidle", timeout=60000)
        time.sleep(2)

        if "login" in page.url.lower():
            raise RuntimeError("登录失败")

        return page.context.storage_state()

    # ---------- 余额 ----------
    def get_balance(self, page):
        page.goto(BALANCE_URL)
        page.wait_for_load_state("networkidle")
        bal = page.locator('p[title="点击显示完整格式"]').text_content().strip()
        spent = page.locator('p.text-3xl.font-bold:not([title])').text_content().strip()
        self.log(f"🏦 余额: {bal} | 已消费: {spent}", "INFO")

    # ---------- 主流程 ----------
    def run(self):
        self.log("Leaflow 多账号任务启动", "STEP")
        accounts = self.config.get_value("LF_INFO") or []
        proxies = self.config.get_value("PROXY_INFO") or []
        lf_locals = self.secret.load() or {}
        new_sessions = {}

        for account, proxy in zip(accounts, proxies):
            username = account["username"]
            password = account["password"]

            # 代理检测
            proxy_ok = test_proxy(proxy)
            if not proxy_ok:
                proxy = self.config.get("wz_proxy")
                test_proxy(proxy)

            pw = browser = None
            try:
                storage = lf_locals.get(username)
                pw, browser, ctx, page = self.open_browser(proxy, storage)

                if not storage or not self.cookies_ok(page):
                    try:
                        storage = self.login_and_get_storage(page, username, password)
                        new_sessions[username] = storage
                    except Exception as e:
                        self.capture_and_notify(page, username, str(e))
                        continue

                self.get_balance(page)

            finally:
                if browser:
                    browser.close()
                if pw:
                    pw.stop()
                if self.gost_proc:
                    self.gost_proc.terminate()
                    self.gost_proc = None

        if new_sessions:
            encoded = {k: base64.b64encode(str(v).encode()).decode() for k, v in new_sessions.items()}
            self.secret.update(encoded)

        self.notifier.send(
            title="Leaflow 自动任务完成",
            content="\n".join(self.logs)
        )


if __name__ == "__main__":
    LeaflowTask().run()
