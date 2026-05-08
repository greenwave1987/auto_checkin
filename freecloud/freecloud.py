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

# 常量配置
LOGIN_URL = "https://freecloud.ltd/login"
DASHBOARD_URL = "https://freecloud.ltd/user"
CHECKIN_URL = "https://checkin.freecloud.ltd/"

# ==================== 工具函数 ====================
def mask_email(email: str):
    if not email or "@" not in email: return email
    name, domain = email.split("@", 1)
    return f"{name[:3]}***@{domain}"

def decode_storage(b64_str):
    if not b64_str: return None
    try:
        return json.loads(base64.b64decode(b64_str).decode())
    except Exception:
        return None

def encode_storage(storage_dict):
    if not storage_dict: return ""
    return base64.b64encode(json.dumps(storage_dict).encode()).decode()

# ==================== 物理点击逻辑脚本 ====================
_SOLVED_JS = "() => { var i = document.querySelector('input[name=\"cf-turnstile-response\"]'); return !!(i && i.value && i.value.length > 20); }"

_COORDS_JS = """
() => {
    var f = document.querySelector('iframe[src*="challenges"]');
    if (f) {
        var r = f.getBoundingClientRect();
        if (r.width > 0 && r.height > 0)
            return {cx: Math.round(r.x + 30), cy: Math.round(r.y + r.height / 2)};
    }
    return null;
}
"""

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

    async def capture_and_send(self, page, caption):
        """截图并立即发送到 Telegram"""
        try:
            img_bytes = await page.screenshot(full_page=False)
            # 这里的 self.notifier.send_photo 是基于你 engine 里的实现
            # 如果你的 notifier 只有 send_msg，请确保它支持发送字节流
            await self.notifier.send_photo(img_bytes, caption=f"📸 {caption}")
        except Exception as e:
            self.log(f"截图发送失败: {str(e)}", "WARN")

    async def start_gost_proxy(self, proxy):
        port = 10801
        remote = f"socks5://{proxy['username']}:{proxy['password']}@{proxy['server']}:{proxy['port']}"
        self.log(f"启动 Gost 转接: 127.0.0.1:{port}", "STEP")
        self.gost_proc = subprocess.Popen(
            ["./gost", "-L", f":{port}", "-F", remote],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        await asyncio.sleep(3)
        return f"socks5://127.0.0.1:{port}"

    async def wait_for_turnstile(self, page):
        self.log("正在探测 Cloudflare 验证码...", "STEP")
        try:
            for attempt in range(12):
                if await page.evaluate(_SOLVED_JS):
                    self.log("Cloudflare 验证已自动/手动通过", "SUCCESS")
                    return True
                
                coords = await page.evaluate(_COORDS_JS)
                if coords:
                    ax, ay = coords["cx"], coords["cy"]
                    target_y = ay + 80 
                    self.log(f"发现验证框 ({ax}, {ay})，执行物理点击...", "STEP")
                    try:
                        subprocess.run(["xdotool", "mousemove", str(ax), str(target_y), "click", "1"], check=True)
                    except:
                        await page.mouse.click(ax, ay)
                    
                    await asyncio.sleep(2)
                    await self.capture_and_send(page, "尝试点击验证码")
                
                await asyncio.sleep(5)
            return False
        except Exception as e:
            self.log(f"验证模块异常: {str(e)}", "WARN")
            return False

    async def init_browser(self, p_instance, proxy_info, storage_state):
        launch_args = {
            "headless": False, 
            "args": [
                "--no-sandbox", 
                "--disable-blink-features=AutomationControlled",
                "--window-size=1280,800"
            ]
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
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        return browser, page

    async def run(self):
        self.log("Freecloud 任务启动 [网络增强版]", "STEP")
        accounts = self.config.get_value("FC_INFO") or []
        proxies = self.config.get_value("WZ_INFO") or []
        local_secrets = self.secret.load() or {}
        new_sessions = {}

        async with playwright.async_api.async_playwright() as p:
            for i, account in enumerate(accounts):
                user = account.get("username")
                pwd = account.get("password")
                proxy = proxies[i] if i < len(proxies) else None
                
                print(f"\n{'='*20} 账号 {i+1} {'='*20}")
                self.log(f"当前账号: {mask_email(user)}", "STEP")
                
                browser = None
                try:
                    test_proxy(proxy)
                    storage = decode_storage(local_secrets.get(user))
                    browser, page = await self.init_browser(p, proxy, storage)
                    
                    self.log("开始执行登录流程...", "WARN")
                    await self.do_login(page, user, pwd)
                    
                    state = await page.context.storage_state()
                    new_sessions[user] = encode_storage(state)
                    self.log("Session 已保存", "SUCCESS")

                    await self.do_checkin(page, user)

                except Exception:
                    err_msg = traceback.format_exc()
                    self.log(f"任务执行异常: {err_msg}", "ERROR")
                    await self.capture_and_send(page, f"任务异常: {user}")
                finally:
                    if browser: await browser.close()
                    if self.gost_proc:
                        self.gost_proc.terminate()
                        self.gost_proc = None

        if new_sessions:
            local_secrets.update(new_sessions)
            self.secret.update(local_secrets)
            self.log("多账号 Session 已回写至 GitHub Secrets", "SUCCESS")
        
        self.log("所有任务执行完毕", "STEP")

    async def do_login(self, page, user, pwd):
        self.log(f"登录目标: {LOGIN_URL}", "STEP")
        try:
            await page.goto(LOGIN_URL, wait_until="commit", timeout=90000)
            await asyncio.sleep(5)
            await self.capture_and_send(page, f"进入登录页: {mask_email(user)}")
        except Exception as e:
            self.log(f"页面加载触发异常: {str(e)}", "WARN")
            await asyncio.sleep(10)

        await self.wait_for_turnstile(page)
        
        try:
            username_input = page.locator('input[name="username"]')
            await username_input.wait_for(state="visible", timeout=30000)
            
            await username_input.fill(user)
            await page.locator('input[name="password"]').fill(pwd)
            
            try:
                placeholder = await page.locator('input[placeholder*="="]').get_attribute("placeholder")
                if placeholder:
                    nums = re.findall(r'\d+', placeholder)
                    if len(nums) >= 2:
                        ans = str(int(nums[0]) + int(nums[1]))
                        await page.locator('input[placeholder*="="]').fill(ans)
                        self.log(f"数学验证码: {ans}", "SUCCESS")
            except: pass

            await self.capture_and_send(page, "填充表单完毕，准备点击登录")
            await page.locator('button:has-text("点击登录"), button:has-text("登录")').click()
            
            await page.wait_for_url(re.compile(r".*/user|.*/dashboard"), timeout=60000)
            await self.capture_and_send(page, "登录成功，进入面板")
            self.log("登录成功！", "SUCCESS")
            
        except Exception as e:
            await self.capture_and_send(page, f"登录失败详情: {user}")
            raise e

    async def do_checkin(self, page, user):
        self.log(f"访问签到地址: {CHECKIN_URL}", "STEP")
        try:
            await page.goto(CHECKIN_URL, wait_until="commit", timeout=60000)
            await asyncio.sleep(6)
            await self.capture_and_send(page, "进入签到页面")
            await self.wait_for_turnstile(page)
            
            checkin_selectors = [
                'button:has-text("每日签到")',
                'button:has-text("点我签到")',
                '.checkin-btn',
                '#checkin'
            ]
            
            if await page.locator('text=今日已签到').count() > 0:
                self.log("今日已签到，跳过", "SUCCESS")
                await self.capture_and_send(page, "检查结果：今日已签到")
                return

            for selector in checkin_selectors:
                btn = page.locator(selector)
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click()
                    self.log(f"点击签到按钮: {selector}", "SUCCESS")
                    await asyncio.sleep(3)
                    await self.capture_and_send(page, "点击签到后状态")
                    return
            
            self.log("未找到可用签到按钮", "WARN")
            await self.capture_and_send(page, "未找到签到按钮")
        except Exception as e:
            self.log(f"签到异常: {str(e)}", "ERROR")
            await self.capture_and_send(page, "签到过程异常")

if __name__ == "__main__":
    asyncio.run(FreecloudTask().run())
