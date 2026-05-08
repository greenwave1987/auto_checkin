import os
import io
import sys
import time
import re
import matplotlib.pyplot as plt
from datetime import datetime, timedelta, timezone
import base64
import json
import socket
import subprocess
import asyncio

# 核心导入：确保从 async_api 导入函数
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from playwright_stealth import stealth

# ==================== 模拟环境依赖（请确保你的 engine 文件夹存在） ====================
try:
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, BASE_DIR)
    from engine.notify import TelegramNotifier
    from engine.main import ConfigReader, SecretUpdater, test_proxy, to_beijing_time
except ImportError:
    # 这里的 fallback 仅用于防止本地调试崩溃
    class Mock: pass
    ConfigReader = SecretUpdater = TelegramNotifier = Mock

# 全局配置
plt.switch_backend('Agg') 
LOGIN_URL = "https://freecloud.ltd/login"
DASHBOARD_URL = "https://freecloud.ltd/server/lxc"
CHECKIN_URL = "https://checkin.freecloud.ltd/"
SCREENSHOT_DIR = "/tmp/freecloud_fail"

# ==================== 工具函数 ====================
def mask_email(email: str):
    if not email or "@" not in email: return "***"
    name, domain = email.split("@", 1)
    return f"{name[:2]}***@{domain}"

def decode_storage(b64_str):
    if not b64_str: return None
    try:
        return json.loads(base64.b64decode(b64_str).decode())
    except: return None

def encode_storage(storage):
    return base64.b64encode(json.dumps(storage).encode()).decode()

# ==================== 核心逻辑类 ====================
class FreecloudTask:
    def __init__(self):
        self.config = ConfigReader()
        self.logs = []
        self.notifier = TelegramNotifier(self.config)
        self.secret = SecretUpdater("FREECLOUD_LOCALS", config_reader=self.config)
        self.gost_proc = None
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)

    def log(self, msg, level="INFO"):
        icons = {"INFO": "ℹ️", "SUCCESS": "✅", "ERROR": "❌", "WARN": "⚠️", "STEP": "🔹"}
        line = f"{icons.get(level,'•')} {msg}"
        print(line, flush=True)
        self.logs.append(line)

    async def start_gost_proxy(self, proxy):
        """为需要账号密码的 SOCKS5 代理启动 Gost 转接"""
        port = 10801 # 固定或动态端口
        remote = f"socks5://{proxy['username']}:{proxy['password']}@{proxy['server']}:{proxy['port']}"
        self.log(f"启动 Gost 转接 -> 本地 127.0.0.1:{port}", "STEP")
        self.gost_proc = subprocess.Popen(
            ["./gost", "-L", f":{port}", "-F", remote],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        await asyncio.sleep(2)
        return f"socks5://127.0.0.1:{port}"

    async def init_browser(self, playwright, proxy_info, storage_state):
        """初始化浏览器上下文"""
        launch_args = {
            "headless": True,
            "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        }

        # 处理代理逻辑
        if proxy_info:
            if proxy_info.get("username"): # 需要认证
                proxy_url = await self.start_gost_proxy(proxy_info)
                launch_args["proxy"] = {"server": proxy_url}
            else: # 无认证
                launch_args["proxy"] = {"server": f"socks5://{proxy_info['server']}:{proxy_info['port']}"}

        browser = await playwright.chromium.launch(**launch_args)
        context = await browser.new_context(
            storage_state=storage_state,
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        await stealth(page)
        return browser, page

    async def login_flow(self, page, user, pwd):
        """执行登录流程"""
        self.log(f"正在登录账号: {mask_email(user)}", "STEP")
        await page.goto(LOGIN_URL, wait_until="networkidle")
        
        await page.locator('input[name="username"]').fill(user)
        await page.locator('input[name="password"]').fill(pwd)
        
        # 识别简单加法验证码
        try:
            captcha_box = page.locator('input[placeholder*="="]')
            placeholder = await captcha_box.get_attribute("placeholder")
            nums = re.findall(r'\d+', placeholder)
            if len(nums) >= 2:
                res = str(int(nums[0]) + int(nums[1]))
                await captcha_box.fill(res)
                self.log(f"验证码识别: {nums[0]}+{nums[1]}={res}", "INFO")
        except: pass

        await page.locator('button:has-text("点击登录")').click()
        await page.wait_for_url(re.compile(r".*/dashboard|.*/index"), timeout=30000)
        self.log("登录跳转成功", "SUCCESS")

    async def run(self):
        self.log("freecloud 多账号任务启动", "STEP")
        accounts = self.config.get_value("FC_INFO") or []
        proxies = self.config.get_value("WZ_INFO") or []
        local_secrets = self.secret.load() or {}
        updated_sessions = {}

        # 核心修复点：明确调用 async_playwright()
        async with async_playwright() as p:
            for i, account in enumerate(accounts):
                user = account.get("username")
                pwd = account.get("password")
                proxy = proxies[i] if i < len(proxies) else None
                
                print("\n" + "="*40)
                self.log(f"任务 {i+1}: {mask_email(user)}", "STEP")
                
                browser = None
                try:
                    storage = decode_storage(local_secrets.get(user))
                    browser, page = await self.init_browser(p, proxy, storage)
                    
                    # 检查是否需要重新登录
                    await page.goto(DASHBOARD_URL, timeout=60000)
                    await asyncio.sleep(3)
                    
                    if "login" in page.url:
                        self.log("Session 过期或首次登录", "WARN")
                        await self.login_flow(page, user, pwd)
                        # 登录成功后标记需要保存 session
                        new_state = await page.context.storage_state()
                        updated_sessions[user] = encode_storage(new_state)
                    else:
                        self.log("Session 有效", "SUCCESS")

                    # 执行签到逻辑
                    await self.do_checkin_process(page, user)

                except Exception as e:
                    self.log(f"账号异常: {str(e)}", "ERROR")
                    # 可以在这里增加截图逻辑
                finally:
                    if browser: await browser.close()
                    if self.gost_proc: 
                        self.gost_proc.terminate()
                        self.gost_proc = None

        # 任务结束同步 Secrets
        if updated_sessions:
            local_secrets.update(updated_sessions)
            self.secret.update(local_secrets)
            self.log("GitHub Secrets 已更新", "SUCCESS")

    async def do_checkin_process(self, page, user):
        """签到动作与报表生成"""
        self.log("开始签到流程...", "STEP")
        await page.goto(CHECKIN_URL, wait_until="networkidle")
        await asyncio.sleep(5)
        
        # 检查是否已签到
        if await page.locator('text=今日已签到').count() > 0:
            self.log("今日已完成签到", "SUCCESS")
        else:
            btn = page.locator('button.checkin-btn')
            if await btn.is_visible():
                await btn.click()
                self.log("点击签到按钮成功", "SUCCESS")
                await asyncio.sleep(3)
        
        # 这里可以调用你之前的 process_freecloud_api 生成图表并发送 TG
        self.log(f"账号 {mask_email(user)} 处理完毕", "INFO")

if __name__ == "__main__":
    asyncio.run(FreecloudTask().run())
