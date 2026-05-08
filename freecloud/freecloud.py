import os
import io
import sys
import time
import re  # 修复缺失的正则库
import matplotlib.pyplot as plt
from datetime import datetime, timedelta, timezone
import base64
import json
import socket
import subprocess
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError # 改为异步
from playwright_stealth import stealth
import asyncio

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from engine.notify import TelegramNotifier
from engine.main import ConfigReader, SecretUpdater, test_proxy, to_beijing_time

plt.switch_backend('Agg') 
LOGIN_URL = "https://freecloud.ltd/login"
DASHBOARD_URL = "https://freecloud.ltd/server/lxc"
BALANCE_URL = "https://freecloud.ltd/balance"
CHECKIN_URL = "https://checkin.freecloud.ltd/"
SCREENSHOT_DIR = "/tmp/freecloud_fail"


# ==================== 工具函数 ====================
def mask_email(email: str):
    if "@" not in email:
        return "***"
    name, domain = email.split("@", 1)
    return f"{name[:2]}***{name[-2:]}@{domain}"

def mask_name(name: str):
    return f"{name[:2]}***{name[-2:]}"

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
class freecloudTask:
    def __init__(self):
        self.config = ConfigReader()
        self.logs = []
        self.notifier = TelegramNotifier(self.config)
        self.secret = SecretUpdater("FREECLOUD_LOCALS", config_reader=self.config)
        self.gost_proc = None
        self.user = "Unknown" # 初始化
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
    async def open_browser(self, proxy, storage):
        self.log("启动 Playwright 浏览器", "STEP")
        pw = await async_playwright().start() # 异步启动

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

        browser = await pw.chromium.launch(**launch_args)
        context = await browser.new_context(
            storage_state=storage,
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 Chrome/128.0.0.0"
        )

        page = await context.new_page()
        await stealth(page)
        return pw, browser, page

    # ---------- 截图 ----------
    async def capture_and_notify(self, page, user, reason):
        path = f"{SCREENSHOT_DIR}/{user}_{int(time.time())}.png"
        try:
            await page.screenshot(path=path, full_page=True, timeout=60000)
        except PlaywrightTimeoutError:
            self.log("截图超时，跳过截图", "WARN")
        self.notifier.send(
            title=f"ℹ️ FreeCloud 登录\n", content=f"账号: {mask_name(user)}\n原因: {reason}", image_path=path
        )

    # ---------- 登录 ----------
    async def do_login(self, page, user, pwd):
        self.log(f"打开登录页: {LOGIN_URL}", "STEP")
        await page.goto(LOGIN_URL, wait_until="networkidle")
    
        self.log(f"输入账号: {mask_email(user)}", "INFO")
        await page.locator('input[name="username"]').fill(user)
    
        self.log(f"输入密码: {mask_password(pwd)}", "INFO")
        await page.locator('input[name="password"]').fill(pwd)
    
        try:
            self.log("正在识别数学验证码...", "STEP")
            captcha_input = page.locator('input[placeholder*="="]')
            captcha_text = await captcha_input.get_attribute("placeholder")
            
            nums = re.findall(r'\d+', captcha_text)
            if len(nums) >= 2:
                result = str(int(nums[0]) + int(nums[1]))
                await captcha_input.fill(result)
                self.log(f"验证码计算成功: {nums[0]} + {nums[1]} = {result}", "INFO")
            else:
                self.log("无法解析验证码文本", "ERROR")
        except Exception as e:
            self.log(f"验证码处理异常: {e}", "WARN")
    
        try:
            self.log("勾选法律声明协议", "STEP")
            await page.locator('input[type="checkbox"]').check()
        except Exception:
            self.log("未找到协议复选框", "WARN")
    
        self.log("点击登录按钮", "STEP")
        await page.locator('button:has-text("点击登录")').click()
        
        try:
            await page.wait_for_url(re.compile(r".*/index|.*/dashboard"), timeout=30000)
        except Exception:
            pass
    
        if "login" in page.url.lower():
            await page.screenshot(path="login_failed.png")
            raise RuntimeError("登录失败：页面未跳转，请检查账号或验证码")
    
        self.log("登录成功", "SUCCESS")

    # ---------- 验证 storage ----------
    async def ensure_login(self, page, user, pwd, max_retry=5):
        for attempt in range(1, max_retry + 1):
            try:
                self.log(f"验证登录状态 (第 {attempt}/{max_retry} 次)", "INFO")
                await page.goto(DASHBOARD_URL, timeout=60000)
                await page.wait_for_load_state("domcontentloaded", timeout=60000)
                await page.wait_for_timeout(5000) # 替换 time.sleep
    
                current_url = page.url.lower()
                if "login" in current_url:
                    self.log("storage 已失效，重新登录", "WARN")
                    await self.do_login(page, user, pwd)
                    return True

                self.log("storage 有效，跳过登录", "SUCCESS")
                return False
    
            except Exception as e:
                self.log(f"验证异常: {str(e)}", "ERROR")
                if attempt < max_retry:
                    self.log("等待后重试...", "WARN")
                    await page.wait_for_timeout(3000)
                    continue
                else:
                    self.log("多次失败，强制重新登录", "ERROR")
                    await self.do_login(page, user, pwd)
                    return True
    
    # ---------- 获取金额信息 ----------
    async def get_balance_data(self, page, max_retry=5):
        api_script = """
        async () => {
            try {
                const response = await fetch("https://freecloud.ltd/balance", {
                    headers: {
                        "x-inertia": "true",
                        "x-inertia-version": "1da8f358bacd543adbf104c91fa91267",
                        "x-requested-with": "XMLHttpRequest"
                    },
                    method: "GET"
                });
                return {
                    status: response.status,
                    ok: response.ok,
                    data: response.ok ? await response.json() : null
                };
            } catch (err) {
                return { status: -1, ok: false, error: err.toString() };
            }
        }
        """
        for attempt in range(1, max_retry + 1):
            try:
                self.log(f"获取余额信息 (第 {attempt}/{max_retry} 次)", "STEP")
                await page.goto(DASHBOARD_URL, timeout=60000)
                await page.wait_for_load_state("domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2000)
                result = await page.evaluate(api_script)
                if result is None: raise Exception("返回数据为空")
                if result.get("status") == -1: raise Exception(result.get("error"))
                if result.get("status") in [401, 403]:
                    self.log("检测到登录失效", "WARN")
                    return "LOGIN_EXPIRED"
                if result.get("ok"):
                    self.log("余额信息获取成功", "SUCCESS")
                    return result.get("data")
                raise Exception(f"HTTP {result.get('status')}")
            except Exception as e:
                self.log(f"获取失败: {str(e)}", "WARN")
                if attempt < max_retry:
                    await page.wait_for_timeout(3000)
                    continue
                else: return None

    # ---------- 签到状态检查 ----------
    async def get_checkin_info(self, page):
        raw_info = await self.get_balance_data(page)
        if raw_info and raw_info != "LOGIN_EXPIRED":
            report = self.process_freecloud_api(raw_info)
            self.user = report['username']
            if report['is_checked_today']:
                self.log(f"今日已签到 (用户: {mask_name(report['username'])}, 余额: {report['balance']})", "SUCCESS")
                status_emoji = "✅" if report["is_checked_today"] else "❌"
                msg = (
                    f"📊 **freecloud 资产报告**\n"
                    f"👤 用户: {mask_name(report['username'])}\n"
                    f"💰 余额: {report['balance']}\n"
                    f"📉 已用: {report['consumed']}\n"
                    f"🕒 签到: {report['last_checkin_time']}\n"
                    f"💴 奖励: {report['last_checkin_amount']}\n"
                    f"📅 今日: {status_emoji}"
                )
                if report["chart_buf"]:
                    temp_chart_path = f"{SCREENSHOT_DIR}/chart_{report['username']}.png"
                    try:
                        with open(temp_chart_path, "wb") as f:
                            f.write(report["chart_buf"].getbuffer())
                        self.notifier.send(title=f"freecloud 签到报告", content=msg, image_path=temp_chart_path)
                        if os.path.exists(temp_chart_path): os.remove(temp_chart_path)
                    except Exception as e:
                        self.log(f"图片保存或发送失败: {e}", "WARN")
                        self.notifier.send(title="freecloud 签到报告", content=msg)
                else:
                    self.notifier.send(title="freecloud 签到报告", content=msg)
                return True
            else:
                self.log(f"今日还未签到!", "WARN")
        return False

    # ---------- 执行签到 ----------
    async def do_checkin(self, page):
        if await self.get_checkin_info(page):
            return
              
        self.log("API 显示未签到，准备执行点击签到...", "STEP")
        checkin_btn_selector = 'button.checkin-btn'
        success_text_selector = 'div.mt-2.mb-1.text-muted.small:has-text("今日已签到")'
        
        for attempt in range(15):
            try:
                self.log(f"第 {attempt+1} 次尝试访问签到页: {CHECKIN_URL}", "STEP")
                await page.goto(CHECKIN_URL, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(15000)
                
                if await page.locator(success_text_selector).count() > 0:
                    self.log("页面检测到今日已签到", "SUCCESS")
                    await self.capture_and_notify(page, self.user, "今日已签到!")
                    await self.get_checkin_info(page)
                    return

                self.log("等待签到按钮出现...", "INFO")
                btn = await page.wait_for_selector(checkin_btn_selector, state="visible", timeout=60000)
                
                if btn:
                    self.log("发现签到按钮，执行点击", "SUCCESS")
                    await page.wait_for_timeout(5000)
                    await btn.click()
                    
                    for _ in range(15):
                        if await page.locator(success_text_selector).count() > 0:
                            self.log("签到确认成功", "SUCCESS")
                            break
                        await page.wait_for_timeout(5000)
                    
                    await self.capture_and_notify(page, self.user, "签到状态!")
                    if await self.get_checkin_info(page):
                        return
                    raise RuntimeError("点击了按钮但状态未更新")

            except Exception as e:
                self.log(f"第 {attempt+1} 次尝试异常: {str(e)}", "WARN")
                if attempt < 14:
                    await page.wait_for_timeout(10000)
                else:
                    await self.capture_and_notify(page, self.user, f"签到最终失败: {str(e)}")
                    raise RuntimeError("签到流程重试耗尽")

    # ---------- 数据处理与图表生成 ----------
    def process_freecloud_api(self, json_data):
        props = json_data.get("props", {})
        user_info = props.get("auth", {}).get("user", {})
        records = props.get("records", {}).get("data", [])
        res = {
            "username": user_info.get("name", "Unknown"),
            "balance": f'{float(props.get("balance", 0)):.2f}',
            "consumed": f'{float(props.get("totalConsumed", 0)):.2f}',
            "last_checkin_time": "无记录",
            "last_checkin_amount": "无记录",
            "is_checked_today": False,
            "daily_history": {},
            "chart_buf": None 
        }
        now_bj = datetime.now(timezone(timedelta(hours=8)))
        today_str = now_bj.strftime("%Y-%m-%d")
        if records:
            last_dt = to_beijing_time(records[0].get("created_at"))
            if last_dt: res["last_checkin_time"] = last_dt.strftime("%Y-%m-%d %H:%M:%S")
            for r in reversed(records):
                remark = r.get("remark", "")
                if "奖励" in remark or "签到" in remark:
                    bj_dt = to_beijing_time(r.get("created_at"))
                    if bj_dt:
                        date_key = bj_dt.strftime("%Y-%m-%d")
                        amount = float(r.get("amount", 0))
                        res["daily_history"][date_key] = res["daily_history"].get(date_key, 0) + amount
                        if date_key == today_str: res["is_checked_today"] = True
            res["last_checkin_amount"] = f'{ float(res["daily_history"].get(today_str, 0)):.2f}'
            
        if res["daily_history"]:
            plt.figure(figsize=(10, 5))
            dates = list(res["daily_history"].keys())[-12:]
            amounts = [res["daily_history"][d] for d in dates]
            plt.plot(dates, amounts, marker='o', color='#10a37f', linewidth=2)
            plt.fill_between(dates, amounts, color='#10a37f', alpha=0.1)
            plt.title(f"Check-in Rewards: {mask_name(res['username'])}", fontsize=12)
            plt.xticks(rotation=30)
            plt.grid(True, linestyle=':', alpha=0.6)
            plt.tight_layout()
            buf = io.BytesIO()
            plt.savefig(buf, format='png')
            buf.seek(0)
            plt.close()
            res["chart_buf"] = buf
        return res

    # ---------- 主流程 ----------
    async def run(self):
        self.log("freecloud 多账号任务启动", "STEP")
        accounts = self.config.get_value("FC_INFO") or []
        proxies = self.config.get_value("WZ_INFO") or []
        lf_locals = self.secret.load() or {}
        new_sessions = {}

        for account, proxy in zip(accounts, proxies):
            page = None
            browser = None
            pw = None
            try:
                print("\n" + "="*50)
                user = account["username"]
                pwd = account["password"]
                self.log(f"开始处理账号: {mask_email(user)}", "STEP")
                test_proxy(proxy)
                storage = decode_storage(lf_locals[user]) if user in lf_locals else None
                try:
                    pw, browser, page = await self.open_browser(proxy, storage)
                    refreshed = await self.ensure_login(page, user, pwd)
                    await self.do_checkin(page)
                    if refreshed or not storage:
                        self.log("更新 storage", "STEP")
                        new_sessions[user] = await page.context.storage_state()
                except Exception as e:
                    self.log(f"{mask_email(user)} 登录异常: {e}", "ERROR")
                    if page: await self.capture_and_notify(page, user, str(e))
                finally:
                    if browser: await browser.close()
                    if pw: await pw.stop()
                    if self.gost_proc:
                        self.gost_proc.terminate()
                        self.gost_proc = None
            except Exception as e:
                self.log(f"处理账号 {user} 时发生未预期错误: {e}", "ERROR")
            
        if new_sessions:
            self.log("准备回写 GitHub Secret", "STEP")
            encoded = {k: encode_storage(v) for k, v in new_sessions.items()}
            self.secret.update(encoded)
            self.log("Secret 回写成功", "SUCCESS")
        self.log("全部任务结束", "STEP")

if __name__ == "__main__":
    asyncio.run(freecloudTask().run())
