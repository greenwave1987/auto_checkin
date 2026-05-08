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
DASHBOARD_URL = "https://freecloud.ltd/user"  # 更改为更通用的 /user 地址
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

    async def start_gost_proxy(self, proxy):
        port = 10801
        remote = f"socks5://{proxy['username']}:{proxy['password']}@{proxy['server']}:{proxy['port']}"
        self.log(f"启动 Gost 转接: 127.0.0.1:{port}", "STEP")
        self.gost_proc = subprocess.Popen(
            ["./gost", "-L", f":{port}", "-F", remote],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        await asyncio.sleep(3) # 给隧道建立预留时间
        return f"socks5://127.0.0.1:{port}"

    async def wait_for_turnstile(self, page):
        """处理 Cloudflare Turnstile 验证 (物理点击版)"""
        self.log("正在探测 Cloudflare 验证码...", "STEP")
        try:
            for attempt in range(12):
                if await page.evaluate(_SOLVED_JS):
                    self.log("Cloudflare 验证已自动/手动通过", "SUCCESS")
                    return True
                
                coords = await page.evaluate(_COORDS_JS)
                if coords:
                    ax, ay = coords["cx"], coords["cy"]
                    # 针对 xvfb 环境的 Y 轴偏移补偿 (浏览器顶栏高度)
                    target_y = ay + 80 
                    self.log(f"发现验证框 ({ax}, {ay})，执行物理点击...", "STEP")
                    try:
                        # 核心：使用系统 xdotool 绕过所有检测
                        subprocess.run(["xdotool", "mousemove", str(ax), str(target_y), "click", "1"], check=True)
                    except:
                        await page.mouse.click(ax, ay)
                
                await asyncio.sleep(5) # 每 5 秒轮询一次
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
                    
                    # 1. 尝试进入面板判断状态 (激进导航)
                    try:
                        self.log("访问控制台以验证 Session...", "STEP")
                        await page.goto(DASHBOARD_URL, wait_until="commit", timeout=60000)
                        await asyncio.sleep(5)
                    except:
                        self.log("控制台加载超时，准备进入登录流程", "WARN")
                    
                    if "login" in page.url.lower() or "auth" in page.url.lower():
                        self.log("需要登录，开始执行登录流程...", "WARN")
                        await self.do_login(page, user, pwd)
                        # 成功后回传 Session
                        state = await page.context.storage_state()
                        new_sessions[user] = encode_storage(state)
                        self.log("Session 已保存", "SUCCESS")
                    else:
                        self.log("Session 有效，跳过登录", "SUCCESS")

                    # 2. 执行签到
                    await self.do_checkin(page, user)

                except Exception:
                    self.log(f"任务执行异常: {traceback.format_exc()}", "ERROR")
                    await page.screenshot(path=f"error_{i}.png")
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
        self.log(f"开始登录流程，目标: {LOGIN_URL}", "STEP")
        try:
            # 1. 延长超时至 90 秒，且使用 commit 模式（只要收到数据包就开始处理）
            # 这能有效解决因为代理慢导致的 30s 超时报错
            await page.goto(LOGIN_URL, wait_until="commit", timeout=90000)
            self.log("登录页面已建立连接，等待渲染...", "INFO")
        except Exception as e:
            self.log(f"页面加载触发异常 (可能未完全加载): {str(e)}", "WARN")
            # 即使报错也强制留出时间给物理点击，因为 commit 模式下页面可能已经部分可见
            await asyncio.sleep(10)

        # 2. 给 Turnstile 验证框预留更长的加载时间
        await asyncio.sleep(8)
        
        # 3. 调用物理点击
        await self.wait_for_turnstile(page)
        
        try:
            # 4. 显式等待输入框出现，而不是依赖 page.goto 的加载状态
            username_input = page.locator('input[name="username"]')
            await username_input.wait_for(state="visible", timeout=30000)
            
            await username_input.fill(user)
            await asyncio.sleep(1)
            await page.locator('input[name="password"]').fill(pwd)
            
            # 数学验证码自动化逻辑 (保持不变)
            try:
                placeholder = await page.locator('input[placeholder*="="]').get_attribute("placeholder")
                if placeholder:
                    nums = re.findall(r'\d+', placeholder)
                    if len(nums) >= 2:
                        ans = str(int(nums[0]) + int(nums[1]))
                        await page.locator('input[placeholder*="="]').fill(ans)
                        self.log(f"数学验证码已自动填充: {ans}", "SUCCESS")
            except: pass

            # 5. 点击登录并等待跳转
            await page.locator('button:has-text("点击登录"), button:has-text("登录")').click()
            
            # 等待跳转到用户中心，同样给足时间
            await page.wait_for_url(re.compile(r".*/user|.*/dashboard"), timeout=60000)
            self.log("登录成功！", "SUCCESS")
            
        except Exception as e:
            self.log(f"登录表单操作失败: {str(e)}", "ERROR")
            # 此时保存截图非常重要，可以查看是卡在了验证码还是账号输入
            await page.screenshot(path=f"login_failed_{user}.png")
            raise e

    async def do_checkin(self, page, user):
        self.log(f"正在访问签到地址: {CHECKIN_URL}", "STEP")
        try:
            await page.goto(CHECKIN_URL, wait_until="commit", timeout=60000)
            await asyncio.sleep(6)
            await self.wait_for_turnstile(page)
            
            # 搜索签到按钮
            checkin_selectors = [
                'button:has-text("每日签到")',
                'button:has-text("点我签到")',
                '.checkin-btn',
                '#checkin'
            ]
            
            if await page.locator('text=今日已签到').count() > 0:
                self.log("今日已签到，跳过", "SUCCESS")
                return

            for selector in checkin_selectors:
                btn = page.locator(selector)
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click()
                    self.log(f"使用选择器 [{selector}] 点击成功", "SUCCESS")
                    await asyncio.sleep(3)
                    return
            
            self.log("未找到可用的签到按钮，可能已签到或页面加载不全", "WARN")
            await page.screenshot(path=f"checkin_failed_{user}.png")
        except Exception as e:
            self.log(f"签到异常: {str(e)}", "ERROR")

if __name__ == "__main__":
    asyncio.run(FreecloudTask().run())
