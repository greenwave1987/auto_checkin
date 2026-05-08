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

import playwright.async_api

# ==================== 环境依赖加载 ====================
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from engine.notify import TelegramNotifier
from engine.main import ConfigReader, SecretUpdater, test_proxy, to_beijing_time

plt.switch_backend('Agg') 

# 常量
LOGIN_URL = "https://freecloud.ltd/login"
DASHBOARD_URL = "https://freecloud.ltd/server/lxc"
CHECKIN_URL = "https://checkin.freecloud.ltd/"

class FreecloudTask:
    def __init__(self):
        self.config = ConfigReader()
        self.logs = []
        self.notifier = TelegramNotifier(self.config)
        self.secret = SecretUpdater("FREECLOUD_LOCALS", config_reader=self.config)
        self.gost_proc = None

    def log(self, msg, level="INFO"):
        icons = {"INFO": "ℹ️", "SUCCESS": "✅", "ERROR": "❌", "WARN": "⚠️", "STEP": "🔹"}
        line = f"{icons.get(level,'•')} {msg}"
        print(line, flush=True)
        self.logs.append(line)

    async def start_gost_proxy(self, proxy):
        port = 10801
        remote = f"socks5://{proxy['username']}:{proxy['password']}@{proxy['server']}:{proxy['port']}"
        self.log(f"启动 Gost 转接: 127.0.0.1:{port}", "STEP")
        self.gost_proc = subprocess.Popen(
            ["./gost", "-L", f":{port}", "-F", remote],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        await asyncio.sleep(2)
        return f"socks5://127.0.0.1:{port}"

    async def wait_for_turnstile(self, page):
        """专门处理 Cloudflare Turnstile 验证"""
        try:
            # 检测是否正在验证
            is_verifying = await page.is_visible("#verifying")
            if is_verifying:
                self.log("检测到 Cloudflare 正在验证，请稍候...", "STEP")
                # 等待 success 容器变为可见，或者 success-text 出现
                # 即使 Session 有效，Cloudflare 也会闪过这个验证
                await page.wait_for_selector("#success:not([style*='display: none'])", timeout=30000)
                self.log("Cloudflare 验证成功", "SUCCESS")
                await asyncio.sleep(2)
            else:
                self.log("无需额外验证", "INFO")
        except Exception:
            self.log("Cloudflare 验证等待超时，尝试强行继续...", "WARN")

    async def init_browser(self, p_instance, proxy_info, storage_state):
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

        browser = await p_instance.chromium.launch(**launch_args)
        context = await browser.new_context(
            storage_state=storage_state,
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        return browser, page

    async def run(self):
        self.log("freecloud 多账号任务启动", "STEP")
        accounts = self.config.get_value("FC_INFO") or []
        proxies = self.config.get_value("WZ_INFO") or []
        local_secrets = self.secret.load() or {}
        new_sessions = {}

        async with playwright.async_api.async_playwright() as p:
            for i, account in enumerate(accounts):
                user = account.get("username")
                pwd = account.get("password")
                proxy = proxies[i] if i < len(proxies) else None
                
                print("\n" + "="*50)
                self.log(f"任务 {i+1}: {user[:3]}***")
                
                browser = None
                try:
                    test_proxy(proxy)
                    storage = decode_storage(local_secrets.get(user))
                    browser, page = await self.init_browser(p, proxy, storage)
                    
                    # 1. 访问主页并处理验证
                    await page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=60000)
                    await self.wait_for_turnstile(page)
                    
                    if "login" in page.url.lower():
                        self.log("Session 过期，开始登录...", "WARN")
                        await self.do_login(page, user, pwd)
                        state = await page.context.storage_state()
                        new_sessions[user] = encode_storage(state)
                        self.log("Session 已保存", "SUCCESS")
                    else:
                        self.log("Session 依然有效", "SUCCESS")

                    # 2. 签到逻辑
                    await self.do_checkin(page, user)

                except Exception:
                    self.log(f"处理失败: {traceback.format_exc()}", "ERROR")
                finally:
                    if browser: await browser.close()
                    if self.gost_proc:
                        self.gost_proc.terminate()
                        self.gost_proc = None

        if new_sessions:
            local_secrets.update(new_sessions)
            self.secret.update(local_secrets)
        
        self.log("任务全部结束", "STEP")

    async def do_login(self, page, user, pwd):
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        await self.wait_for_turnstile(page)
        
        await page.locator('input[name="username"]').fill(user)
        await page.locator('input[name="password"]').fill(pwd)
        
        # 验证码计算
        try:
            placeholder = await page.locator('input[placeholder*="="]').get_attribute("placeholder")
            nums = re.findall(r'\d+', placeholder)
            if len(nums) >= 2:
                ans = str(int(nums[0]) + int(nums[1]))
                await page.locator('input[placeholder*="="]').fill(ans)
        except: pass

        await page.locator('button:has-text("点击登录")').click()
        await page.wait_for_url(re.compile(r".*/dashboard|.*/index"), timeout=30000)

    async def do_checkin(self, page, user):
        max_retries = 3
        for attempt in range(max_retries):
            try:
                self.log(f"访问签到页 (尝试 {attempt+1})...", "STEP")
                await page.goto(CHECKIN_URL, wait_until="domcontentloaded", timeout=60000)
                # 重要：签到子域名同样会有验证
                await self.wait_for_turnstile(page)
                
                await asyncio.sleep(5)
                
                if await page.locator('text=今日已签到').count() > 0:
                    self.log("今日已签到过", "SUCCESS")
                    return
                
                btn = page.locator('button.checkin-btn')
                if await btn.is_visible():
                    await btn.click()
                    self.log("签到成功！", "SUCCESS")
                    await asyncio.sleep(3)
                    return
                else:
                    self.log("未找到签到按钮，可能页面未完全加载", "WARN")
            except Exception as e:
                self.log(f"签到异常: {str(e)}", "WARN")
                await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(FreecloudTask().run())
