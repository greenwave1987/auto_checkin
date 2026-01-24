import os
import sys
import time
import random
import base64
import socket
import subprocess
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from engine.notify import TelegramNotifier
from engine.main import ConfigReader, SecretUpdater, test_proxy

LOGIN_URL = "https://leaflow.net/login"
DASHBOARD_URL = "https://leaflow.net/dashboard"
BALANCE_URL = "https://leaflow.net/balance"
CHECKIN_URL = "https://checkin.leaflow.net/"
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

    def tg_notify(self, msg):
        self.notifier.send("Leaflow 自动登录维护", msg)

    # ---------- Gost ----------
    def start_gost_proxy(self, proxy):
        def free_port():
            s = socket.socket()
            s.bind(("", 0))
            port = s.getsockname()[1]
            s.close()
            return port

        port = free_port()
        server = f"socks5://127.0.0.1:{port}"
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
    def open_browser(self, proxy, storage):
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
        context = browser.new_context(
            storage_state=storage,
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 Chrome/128.0.0.0"
        )

        page = context.new_page()
        return pw, browser, page

    # ---------- 截图 ----------
    def capture_and_notify(self, page, user, reason):
        path = f"{SCREENSHOT_DIR}/{user}_{int(time.time())}.png"
        try:
            page.screenshot(path=path, full_page=True)
        except Exception as e:
            self.log(f"⚠️ 截图失败: {e}", "WARN")
        self.tg_notify(f"❌ Leaflow 登录失败\n账号: {mask_email(user)}\n原因: {reason}")

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

    # ---------- 签到 ----------
    def do_checkin(self, page):
        self.log(f"🔹 打开签到页: {CHECKIN_URL}", "STEP")
        for attempt in range(3):
            try:
                page.goto(CHECKIN_URL, wait_until="load", timeout=120000)
                break
            except PlaywrightTimeoutError:
                self.log(f"⚠️ 第 {attempt+1} 次访问签到页失败，重试中...", "WARN")
                time.sleep(2)
        else:
            raise RuntimeError("访问签到页失败")

        # 等待签到按钮
        try:
            checkin_btn = page.locator('button[name="checkin"]')
            if checkin_btn.is_visible():
                self.log("🔹 点击立即签到按钮", "STEP")
                checkin_btn.click()
                page.wait_for_timeout(60000)  # 等待刷新
        except Exception as e:
            self.log(f"⚠️ 未找到签到按钮或点击失败: {e}", "WARN")

        # 检查是否已签到
        try:
            done_text = page.locator('div.mt-2.mb-1.text-muted.small')
            if "今日已签到" in done_text.text_content():
                self.log("✅ 今日已签到", "SUCCESS")
        except Exception:
            self.log("⚠️ 无法确认签到状态", "WARN")

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

            pw = browser = page = None
            try:
                pw, browser, page = self.open_browser(proxy, lf_locals.get(user))

                if not lf_locals.get(user):
                    self.log("未发现 storage，执行登录", "WARN")
                    new_sessions[user] = self.login_and_get_storage(page, user, pwd)
                else:
                    self.log("✅ storage 有效，跳过登录", "INFO")

                # 打开余额页
                self.log(f"🔹 打开余额页: {BALANCE_URL}", "STEP")
                page.goto(BALANCE_URL)
                page.wait_for_load_state("networkidle")
                try:
                    bal = page.locator('p[title]').text_content().strip()
                    spent = page.locator('p.text-3xl.font-bold:not([title])').text_content().strip()
                    self.log(f"🏦 余额: {bal} | 已消费: {spent}", "INFO")
                except Exception:
                    self.log("⚠️ 无法读取余额信息", "WARN")

                # 执行签到
                self.do_checkin(page)

                # 登录或验证成功后更新 storage
                if user not in new_sessions:
                    new_sessions[user] = page.context.storage_state()

            except Exception as e:
                self.log(f"❌ {user} 登录异常: {e}", "ERROR")
                self.capture_and_notify(page, user, str(e))

            finally:
                try:
                    if browser:
                        browser.close()
                    if pw:
                        pw.stop()
                    if self.gost_proc:
                        self.gost_proc.terminate()
                        self.gost_proc = None
                except Exception:
                    pass

        if new_sessions:
            self.log("📝 准备回写 GitHub Secret", "STEP")
            encoded = {k: base64.b64encode(str(v).encode()).decode() for k, v in new_sessions.items()}
            self.secret.update(encoded)
            self.log("✅ Secret 回写成功", "SUCCESS")

        self.log("🔔 开始发送通知", "STEP")
        self.notifier.send("Leaflow 自动登录维护", "\n".join(self.logs))


if __name__ == "__main__":
    LeaflowTask().run()
