import os
import io
import sys
import time
import re
import base64
import json
import subprocess
import asyncio
import traceback
import playwright.async_api

# ==================== 环境依赖与配置 ====================
# 建议配置环境变量: FREECLOUD_EMAIL, FC_PASSWORD, TG_BOT_TOKEN, TG_CHAT_ID
LOGIN_URL = "https://freecloud.ltd/login"
DASHBOARD_URL = "https://freecloud.ltd/server/lxc"  # 或者 /user
CHECKIN_URL = "https://checkin.freecloud.ltd/"

# ==================== 物理点击 & 验证绕过 JS ====================
# 判断 Turnstile 是否已成功生成 Token
_SOLVED_JS = "() => { var i = document.querySelector('input[name=\"cf-turnstile-response\"]'); return !!(i && i.value && i.value.length > 20); }"

# 获取 Turnstile 验证框的中心坐标
_COORDS_JS = """
() => {
    var f = document.querySelector('iframe[src*="challenges"]');
    if (f) {
        var r = f.getBoundingClientRect();
        // 返回中心坐标点，+30px 偏移量增加点击成功率
        return {cx: Math.round(r.x + 30), cy: Math.round(r.y + r.height / 2)};
    }
    return null;
}
"""

class FreecloudTask:
    def __init__(self):
        # 兼容原本的配置读取逻辑
        self.logs = []
        # 假设你已有这些工具类，如果没有，请保留原有的导入
        # from engine.main import ConfigReader, SecretUpdater...
        
    def log(self, msg, level="INFO"):
        icons = {"INFO": "ℹ️", "SUCCESS": "✅", "ERROR": "❌", "WARN": "⚠️", "STEP": "🔹"}
        print(f"{icons.get(level,'•')} {msg}", flush=True)
        self.logs.append(msg)

    async def handle_cf_turnstile(self, page):
        """核心整合：模拟 SeleniumBase 的物理点击逻辑"""
        self.log("正在探测 Cloudflare 验证 (Turnstile)...", "STEP")
        
        for attempt in range(10):  # 最多等待 10 次尝试
            # 1. 检查是否已经解决
            if await page.evaluate(_SOLVED_JS):
                self.log("Cloudflare 验证已通过 (已检测到 Response)", "SUCCESS")
                return True
            
            # 2. 获取坐标并尝试物理点击
            coords = await page.evaluate(_COORDS_JS)
            if coords:
                ax, ay = coords["cx"], coords["cy"]
                # 针对 Headless 模式在 xvfb 下的偏移补偿 (通常 80px 是浏览器顶栏高度)
                target_y = ay + 80 
                self.log(f"探测到验证码位置 ({ax}, {ay})，执行模拟物理点击...", "STEP")
                
                try:
                    # 调用系统 xdotool 执行真物理点击，绕过所有 JS 检测
                    subprocess.run(["xdotool", "mousemove", str(ax), str(target_y), "click", "1"], check=True)
                except Exception:
                    # 如果系统没安装 xdotool，退而求其次使用 Playwright 模拟点击
                    await page.mouse.click(ax, ay)
                    self.log("xdotool 缺失，使用 Playwright 模拟点击", "WARN")
            
            await asyncio.sleep(3)
        return False

    async def init_browser(self, p_instance):
        """初始化带有反爬参数的浏览器"""
        # 注意：物理点击必须在 headless=False 配合 xvfb 环境下效果最好
        browser = await p_instance.chromium.launch(
            headless=False, 
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled", # 核心：去除 webdriver 特征
                "--window-size=1280,800"
            ]
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        # 注入脚本进一步隐藏特征
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        return browser, context

    async def run_task(self, email, password):
        async with playwright.async_api.async_playwright() as p:
            browser, context = await self.init_browser(p)
            page = await context.new_page()
            
            try:
                self.log(f"正在访问登录页面: {LOGIN_URL}", "STEP")
                await page.goto(LOGIN_URL, wait_until="networkidle")
                
                # 整合验证绕过
                await self.handle_cf_turnstile(page)
                
                # 填充账号
                self.log("正在填充账号信息...", "STEP")
                await page.fill('input[type="email"]', email)
                await asyncio.sleep(0.5)
                await page.fill('input[type="password"]', password)
                
                # 处理数学计算验证码（如果存在）
                try:
                    captcha_input = page.locator('input[placeholder*="="]')
                    if await captcha_input.is_visible():
                        placeholder = await captcha_input.get_attribute("placeholder")
                        nums = re.findall(r'\d+', placeholder)
                        if len(nums) >= 2:
                            ans = str(int(nums[0]) + int(nums[1]))
                            await captcha_input.fill(ans)
                            self.log(f"计算验证码: {nums[0]}+{nums[1]}={ans}", "SUCCESS")
                except: pass

                await page.click('button:has-text("登录")')
                
                # 等待登录成功跳转
                try:
                    await page.wait_for_url(re.compile(r".*/dashboard|.*/user"), timeout=20000)
                    self.log("登录成功！", "SUCCESS")
                except:
                    self.log("登录跳转超时，尝试继续执行", "WARN")

                # 执行签到逻辑
                await self.do_checkin(page)

            except Exception as e:
                self.log(f"运行出错: {str(e)}", "ERROR")
                # 调试用：保存截图
                await page.screenshot(path="error_debug.png")
            finally:
                await browser.close()

    async def do_checkin(self, page):
        self.log(f"跳转至签到链接: {CHECKIN_URL}", "STEP")
        await page.goto(CHECKIN_URL)
        await self.handle_cf_turnstile(page) # 签到页可能也有验证码
        
        await asyncio.sleep(5)
        # 兼容多种按钮文字
        for btn_text in ["每日签到", "点我签到"]:
            btn = page.locator(f'button:has-text("{btn_text}")')
            if await btn.is_visible():
                await btn.click()
                self.log(f"点击按钮 [{btn_text}] 成功", "SUCCESS")
                return
        
        if await page.locator('text=今日已签到').count() > 0:
            self.log("今日已签到，无需重复操作", "SUCCESS")
        else:
            self.log("未发现签到按钮，可能页面布局已更新", "WARN")

# ==================== 执行入口 ====================
if __name__ == "__main__":
    # 示例执行，实际使用时请通过循环处理多账号
    email = os.environ.get("FREECLOUD_EMAIL")
    password = os.environ.get("FC_PASSWORD")
    
    task = FreecloudTask()
    asyncio.run(task.run_task(email, password))
