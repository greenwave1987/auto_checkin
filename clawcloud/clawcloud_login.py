#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import re
import requests
import pyotp
import subprocess
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

# 策略配置
USE_PROXY = True
SIGNIN_URL = "https://console.run.claw.cloud/signin"

# 导入原有类 (获取参数方式严格禁止改动)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
try:
    from engine.main import ConfigReader, SecretUpdater
except ImportError:
    pass

class ClawAutoLogin:
    def __init__(self):
        # --- 保持基准脚本获取参数方式 ---
        self.config = ConfigReader()
        self.accounts = self.config.get_value("GH_INFO") or []
        
        raw_proxy = self.config.get_value("PROXY_INFO")
        if isinstance(raw_proxy, dict) and "value" in raw_proxy:
            self.proxy_list = raw_proxy["value"]
        else:
            self.proxy_list = raw_proxy if isinstance(raw_proxy, list) else []

        self.bot_info = (self.config.get_value("BOT_INFO") or [{}])[0]
        self.tg_token = self.bot_info.get("token")
        self.tg_chat_id = self.bot_info.get("id")

        self.session_updater = SecretUpdater("GH_SESSION", config_reader=self.config)
        self.gost_proc = None
        self.detected_region = None

    def log(self, msg, level="INFO"):
        icons = {"INFO": "ℹ️", "SUCCESS": "✅", "ERROR": "❌", "STEP": "🔹", "WARN": "⚠️", "BLOCK": "🚫"}
        print(f"{icons.get(level, '•')} {msg}")

    # --- 保持基准脚本代理逻辑 ---
    def stop_gost(self):
        if self.gost_proc:
            try:
                self.gost_proc.terminate()
                self.gost_proc = None
            except: pass

    def start_gost(self, proxy_data):
        if not USE_PROXY or not proxy_data: return None
        p_str = f"{proxy_data.get('username')}:{proxy_data.get('password')}@{proxy_data.get('server')}:{proxy_data.get('port')}"
        local_proxy = "http://127.0.0.1:8080"
        try:
            if os.path.exists("./gost"): os.chmod("./gost", 0o755)
            self.gost_proc = subprocess.Popen(
                ["./gost", "-L=:8080", f"-F=socks5://{p_str}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            time.sleep(5)
            return local_proxy
        except:
            self.stop_gost()
            return None

    def process_account(self, idx, account):
        username = account.get("username")
        self.log(f"--- 账号处理开始: {username} ---", "STEP")
        
        current_proxy_data = self.proxy_list[idx] if idx < len(self.proxy_list) else None
        local_proxy = self.start_gost(current_proxy_data)

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-blink-features=AutomationControlled'],
                proxy={"server": local_proxy} if local_proxy else None
            )
            context = browser.new_context(viewport={'width': 1920, 'height': 1080})
            page = context.new_page()

            try:
                # 步骤 1-3 (按基准执行)
                self.log("步骤1: 打开 ClawCloud 登录页", "STEP")
                page.goto(SIGNIN_URL, timeout=60000)
                page.wait_for_load_state('networkidle')
                
                if 'signin' in page.url.lower():
                    self.log("步骤2: 点击 GitHub 按钮", "STEP")
                    page.locator('button:has-text("GitHub"), [data-provider="github"]').first.click()
                    time.sleep(3)
                    
                    self.log("步骤3: 执行 GitHub 认证", "STEP")
                    if 'github.com/login' in page.url:
                        page.fill('input[name="login"]', username)
                        page.fill('input[name="password"]', account.get("password", ""))
                        page.click('input[type="submit"]')
                        time.sleep(5)
                        if "two-factor" in page.url:
                            totp = pyotp.TOTP(account.get("2fasecret", "").replace(" ", "")).now()
                            page.fill('input[id="app_totp"], input[name="otp"]', totp)
                            page.keyboard.press("Enter")
                    
                    if 'authorize' in page.url:
                        page.click('button[name="authorize"]')

                # ====== 步骤4: 等待重定向 (详细日志版) ======
                self.log("步骤4: 等待重定向结果", "STEP")
                try:
                    # 等待返回 claw.cloud 域名
                    page.wait_for_url(re.compile(r".*claw\.cloud.*"), timeout=60000)
                    self.log(f"✅ 重定向成功，最终到达 URL: {page.url}", "SUCCESS")
                except Exception as e:
                    self.log(f"❌ 重定向超时或失败: {str(e)}", "ERROR")
                    return

                # ====== 步骤5: 验证 (详细日志版) ======
                self.log("步骤5: 验证登录有效性", "STEP")
                current_url = page.url
                
                # 5.1 检查是否还停留在登录页
                if 'signin' in current_url.lower():
                    self.log("❌ 验证未通过：依然停留在登录界面", "ERROR")
                    return
                
                # 5.2 检查域名完整性
                if 'claw.cloud' in current_url:
                    self.log(f"✅ 域名验证通过: {current_url}", "SUCCESS")
                else:
                    self.log(f"❓ 警告：当前域名非预想范围: {current_url}", "WARN")

                # 5.3 区域检测日志
                parsed = urlparse(current_url)
                host = parsed.netloc
                if host.endswith('.console.claw.cloud'):
                    self.detected_region = host.split('.')[0]
                    self.log(f"📍 检测到控制台分配区域: 【{self.detected_region}】", "SUCCESS")
                else:
                    self.log("📍 未检测到特定子区域，可能在主控制台页面", "INFO")

                # ====== 步骤6: 正在执行保活操作 (详细日志版) ======
                self.log("步骤6: 正在执行保活操作...", "STEP")
                
                # 6.1 尝试访问 Dashboard
                dashboard_url = f"{parsed.scheme}://{parsed.netloc}/dashboard"
                self.log(f"🔄 正在加载仪表盘进行活跃度上报: {dashboard_url}")
                page.goto(dashboard_url, wait_until="networkidle", timeout=30000)
                
                # 6.2 检查页面元素确保加载成功
                try:
                    # 假设控制台有 "Instances" 或 "User" 相关的文字
                    page.wait_for_selector('text=Console, text=Dashboard', timeout=10000)
                    self.log("✅ 仪表盘元素加载成功，Session 状态活跃", "SUCCESS")
                except:
                    self.log("⚠️ 仪表盘加载缓慢，但页面已跳转", "WARN")

                # 6.3 截图留存
                final_shot = f"success_{username}.png"
                page.screenshot(path=final_shot)
                self.log(f"📸 已保存最终登录截图: {final_shot}", "INFO")

                # 6.4 更新 Session (仅首个账号)
                if idx == 0:
                    cookies = context.cookies()
                    new_s = next((c['value'] for c in cookies if c['name'] == 'user_session'), None)
                    if new_s:
                        self.session_updater.update(new_s)
                        self.log("🔑 GitHub Session Cookie 已同步更新至 Secrets", "SUCCESS")

                self.log(f"🎊 账号 {username} 全流程处理完成", "SUCCESS")

            except Exception as e:
                self.log(f"🔴 运行异常: {str(e)}", "ERROR")
            finally:
                browser.close()
                self.stop_gost()

    def run(self):
        if not self.accounts:
            self.log("未发现账号配置", "ERROR")
            return
        for i, acc in enumerate(self.accounts):
            self.process_account(i, acc)
            if i < len(self.accounts) - 1:
                time.sleep(10)

if __name__ == "__main__":
    ClawAutoLogin().run()
