import os
import sys
import time
import base64
import json
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
    return f"***{ip}" if ip else "***"


def mask_password(pwd: str):
    return "*" * 6 + f"({len(pwd)})"


def decode_storage(b64_str):
    try:
        raw = base64.b64decode(b64_str).decode()
        return json.loads(raw)
    except Exception:
        return None


def encode_storage(storage):
    return base64.b64encode(json.dumps(storage).encode()).decode()


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
            page.screenshot(path=path, full_page=True, timeout=30000)  # 30秒
        except PlaywrightTimeoutError:
            self.log("⚠️ 截图超时，跳过截图", "WARN")
        self.notifier.send(
            
            title=f"❌ Leaflow 登录失败\n",content=f"账号: {mask_email(user)}\n原因: {reason}",image_path=path
        )



    # ---------- 登录 ----------
    def do_login(self, page, user, pwd):
        self.log(f"打开登录页: {LOGIN_URL}", "STEP")
        page.goto(LOGIN_URL)

        self.log(f"输入账号: {mask_email(user)}", "INFO")
        page.fill("#account", user)

        self.log(f"输入密码: {mask_password(pwd)}", "INFO")
        page.fill("#password", pwd)

        try:
            self.log("点击「保持登录状态」", "STEP")
            page.get_by_role("checkbox", name="保持登录状态").click(force=True)
        except Exception:
            self.log("未找到保持登录复选框", "WARN")

        self.log("点击登录按钮", "STEP")
        page.locator('button[type="submit"]').click()
        page.wait_for_load_state("networkidle", timeout=60000)

        if "login" in page.url.lower():
            raise RuntimeError("登录失败")

        self.log("登录成功", "SUCCESS")

    # ---------- 验证 storage ----------
    def ensure_login(self, page, user, pwd):
        page.goto(DASHBOARD_URL)
        page.wait_for_load_state("networkidle")

        if "login" in page.url.lower():
            self.log("storage 已失效，重新登录", "WARN")
            self.do_login(page, user, pwd)
            return True

        self.log("storage 有效，跳过登录", "SUCCESS")
        return False
    # ---------- 获取金额信息 ----------  
    def get_balance_data(self, page):
        self.log("正在通过 API 获取账户余额信息...", "STEP")
        # 注入 fetch 脚本
        api_script = """
        async () => {
            const response = await fetch("https://leaflow.net/balance", {
                "headers": {
                    "x-inertia": "true",
                    "x-inertia-version": "1da8f358bacd543adbf104c91fa91267",
                    "x-requested-with": "XMLHttpRequest"
                },
                "method": "GET"
            });
            return await response.json();
        }
        """
        try:
            data = page.evaluate(api_script)
            props = data.get("props", {})
            
            # 提取余额
            balance = props.get("balance", "0.00")
            
            # 提取最后一条记录判断是否签到
            records = props.get("records", {}).get("data", [])
            is_checked = False
            if records:
                last_remark = records[0].get("remark", "")
                last_time = records[0].get("created_at", "")
                # 如果第一条记录是签到奖励，且时间是今天（UTC），则视为已签到
                if "每日签到奖励" in last_remark:
                    # 简单判断日期是否为今天（根据你返回的数据是 2026-01-24）
                    if time.strftime("%Y-%m-%d") in last_time:
                        is_checked = True
            
            return {"balance": balance, "is_checked": is_checked}
        except Exception as e:
            self.log(f"API 数据获取失败: {e}", "WARN")
            return None
        
    # ---------- 签到 ----------
    def do_checkin(self, page):
        # 1. 先通过 API 获取数据
        info = self.get_balance_data(page)
        
        if info:
            self.log(f"当前余额: {info['balance']}", "INFO")
            if info['is_checked']:
                self.log("✅ API 确认今日已签到，跳过点击", "SUCCESS")
                return
    
        # 2. 如果 API 显示未签到，再执行点击操作
        self.log("API 显示未签到，准备执行点击签到...", "STEP")
        self.log(f"打开签到页: {CHECKIN_URL}", "STEP")
        for attempt in range(3):
            try:
                page.goto(CHECKIN_URL, wait_until="domcontentloaded", timeout=120000)
                break
            except PlaywrightTimeoutError:
                self.log(f"⚠️ 第 {attempt+1} 次访问签到页失败，重试中...", "WARN")
                time.sleep(2)
        else:
            raise RuntimeError("访问签到页失败")
    
        # 先检查是否已经签到
        checked_div = page.locator('div.mt-2.mb-1.text-muted.small', has_text="今日已签到")
        if checked_div.count() > 0:
            self.log("✅ 今日已签到，跳过点击", "SUCCESS")
            return
    
        # 查找立即签到按钮
        btn = page.locator('button.checkin-btn')
        if btn.count() == 0:
            self.log("⚠️ 未发现签到按钮，可能页面未完全加载或已签到", "WARN")
            return
    
        # 点击签到
        self.log("点击「立即签到」按钮", "STEP")
        try:
            btn.first.click(timeout=60000)
            time.sleep(2)
    
            # 点击后再次确认是否签到成功
            checked_div = page.locator('div.mt-2.mb-1.text-muted.small', has_text="今日已签到")
            if checked_div.count() > 0:
                self.log("✅ 签到成功", "SUCCESS")
            else:
                self.log("⚠️ 点击签到按钮后未检测到签到状态", "WARN")
    
        except PlaywrightTimeoutError:
            self.log("⚠️ 点击签到按钮超时，可能页面未完全渲染", "WARN")


    # ---------- 主流程 ----------
    def run(self):
        self.log("Leaflow 多账号任务启动", "STEP")

        accounts = self.config.get_value("LF_INFO") or []
        proxies = self.config.get_value("PROXY_INFO") or []
        lf_locals = self.secret.load() or {}

        new_sessions = {}

        for account, proxy in zip(accounts, proxies):
            try:
                user = account["username"]
                pwd = account["password"]
    
                self.log(f"开始处理账号: {mask_email(user)}", "STEP")
                self.log(f"检测代理: {mask_ip(proxy['server'])}", "STEP")
                test_proxy(proxy)
    
                storage = None
                if user in lf_locals:
                    storage = decode_storage(lf_locals[user])
    
                pw = browser = None
                try:
                    pw, browser, page = self.open_browser(proxy, storage)
    
                    refreshed = self.ensure_login(page, user, pwd)
                    self.do_checkin(page)
    
                    if refreshed or not storage:
                        self.log("更新 storage", "STEP")
                        new_sessions[user] = page.context.storage_state()
    
                except Exception as e:
                    self.log(f"{mask_email(user)} 登录异常: {e}", "ERROR")
                    if page:
                        self.capture_and_notify(page, user, str(e))
    
                finally:
                    if browser:
                        try:
                            browser.close()
                        except Exception:
                            pass
                    if pw:
                        try:
                            pw.stop()
                        except Exception:
                            pass
                    if self.gost_proc:
                        self.gost_proc.terminate()
                        self.gost_proc = None
            except Exception as e:
                self.log(f"处理账号 {user} 时发生未预期错误: {e}", "ERROR")
                # 可以在这里增加一层保护，防止 notifier 本身报错导致崩溃
                try:
                    self.capture_and_notify(page, user, str(e))
                except:
                    pass
            break
            
        if new_sessions:
            self.log("📝 准备回写 GitHub Secret", "STEP")
            encoded = {k: encode_storage(v) for k, v in new_sessions.items()}
            self.secret.update(encoded)
            self.log("✅ Secret 回写成功", "SUCCESS")

        self.log("🔔 开始发送通知", "STEP")
        self.notifier.send(title="Leaflow 自动签到结果", content="\n".join(self.logs))


if __name__ == "__main__":
    LeaflowTask().run()
