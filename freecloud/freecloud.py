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
import traceback

# 强制使用绝对路径导入
import playwright.async_api

# ==================== 环境依赖加载 ====================
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from engine.notify import TelegramNotifier
from engine.main import ConfigReader, SecretUpdater, test_proxy, to_beijing_time

# 图形后端配置
plt.switch_backend('Agg') 

# 常量配置
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
        """处理带账号密码的 SOCKS5 代理"""
        port = 10801
        remote = f"socks5://{proxy['username']}:{proxy['password']}@{proxy['server']}:{proxy['port']}"
        self.log(f"启动 Gost 本地转接: 127.0.0.1:{port}", "STEP")
        self.gost_proc = subprocess.Popen(
            ["./gost", "-L", f":{port}", "-F", remote],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        await asyncio.sleep(2)
        return f"socks5://127.0.0.1:{port}"

    async def init_browser(self, p_instance, proxy_info, storage_state):
        """初始化浏览器"""
        launch_args = {
            "headless": True,
            "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        }

        if proxy_info:
            if proxy_info.get("username"):
                proxy_url = await self.start_gost_proxy(proxy_info)
                launch_args["proxy"] = {"server": proxy_url}
            else:
                launch_args["proxy"] = {"server": f"socks5://{proxy_info['server']}:{proxy_info['port']}"}

        # 这里使用 playwright.async_api.async_playwright() 显式调用
        browser = await p_instance.chromium.launch(**launch_args)
        context = await browser.new_context(
            storage_state=storage_state,
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        # 简单注入防止检测
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        return browser, page

    async def run(self):
        self.log("freecloud 多账号任务启动", "STEP")
        accounts = self.config.get_value("FC_INFO") or []
        proxies = self.config.get_value("WZ_INFO") or []
        local_secrets = self.secret.load() or {}
        new_sessions = {}

        # 【重点修复】显式调用 playwright.async_api.async_playwright()
        # 这种写法最能避开“模块名与函数名同名”导致的调用冲突
        async with playwright.async_api.async_playwright() as p:
            for i, account in enumerate(accounts):
                user = account.get("username")
                pwd = account.get("password")
                proxy = proxies[i] if i < len(proxies) else None
                
                print("\n" + "="*50)
                self.log(f"任务 {i+1}: {mask_email(user)}", "STEP")
                
                browser = None
                try:
                    test_proxy(proxy)
                    storage = decode_storage(local_secrets.get(user))
                    
                    browser, page = await self.init_browser(p, proxy, storage)
                    
                    # 登录验证
                    await page.goto(DASHBOARD_URL, timeout=60000)
                    await asyncio.sleep(5)
                    
                    if "login" in page.url.lower():
                        self.log("Session 已过期，开始重新登录", "WARN")
                        await self.do_login(page, user, pwd)
                        # 记录新 Session
                        state = await page.context.storage_state()
                        new_sessions[user] = encode_storage(state)
                        self.log("Session 已更新", "SUCCESS")
                    else:
                        self.log("Session 依然有效", "SUCCESS")

                    # 执行签到
                    await self.do_checkin(page, user)

                except Exception:
                    error_trace = traceback.format_exc()
                    self.log(f"账号处理失败，详细错误:\n{error_trace}", "ERROR")
                finally:
                    if browser: await browser.close()
                    if self.gost_proc:
                        self.gost_proc.terminate()
                        self.gost_proc = None

        if new_sessions:
            local_secrets.update(new_sessions)
            self.secret.update(local_secrets)
            self.log("GitHub Secrets 回写完成", "SUCCESS")
        
        self.log("全部任务处理结束", "STEP")

    async def do_login(self, page, user, pwd):
        await page.goto(LOGIN_URL, wait_until="networkidle")
        await page.locator('input[name="username"]').fill(user)
        await page.locator('input[name="password"]').fill(pwd)
        
        # 简单数学验证码
        try:
            placeholder = await page.locator('input[placeholder*="="]').get_attribute("placeholder")
            nums = re.findall(r'\d+', placeholder)
            if len(nums) >= 2:
                ans = str(int(nums[0]) + int(nums[1]))
                await page.locator('input[placeholder*="="]').fill(ans)
                self.log(f"自动计算验证码: {nums[0]}+{nums[1]}={ans}", "INFO")
        except: pass

        await page.locator('button:has-text("点击登录")').click()
        # 等待成功跳转
        await page.wait_for_url(re.compile(r".*/dashboard|.*/index"), timeout=30000)

    async def do_checkin(self, page, user):
        self.log("跳转签到页...", "STEP")
        await page.goto(CHECKIN_URL, wait_until="networkidle")
        await asyncio.sleep(5)
        
        if await page.locator('text=今日已签到').count() > 0:
            self.log("检测到今日已签到过", "SUCCESS")
        else:
            btn = page.locator('button.checkin-btn')
            if await btn.is_visible():
                await btn.click()
                self.log("点击签到按钮成功", "SUCCESS")
                await asyncio.sleep(3)
            else:
                self.log("未找到签到按钮，可能已签到或页面结构变化", "WARN")

if __name__ == "__main__":
    # 增加简单的异常捕获，确保 main 运行正常
    try:
        asyncio.run(FreecloudTask().run())
    except Exception as e:
        print(f"CRITICAL ERROR: {e}")
        traceback.print_exc()
