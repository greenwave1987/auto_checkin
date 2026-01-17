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
        # 注意：如果日志显示“代理没有了”，请检查 PROXY_INFO 配置
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
                # 步骤 1: 访问登录页
                self.log("步骤1: 打开 ClawCloud 登录页", "STEP")
                page.goto(SIGNIN_URL, timeout=60000)
                page.wait_for_load_state('networkidle')
                
                if 'signin' in page.url.lower():
                    # 步骤 2: 点击 GitHub
                    self.log("步骤2: 点击 GitHub 按钮", "STEP")
                    page.locator('button:has-text("GitHub"), [data-provider="github"]').first.click()
                    time.sleep(3)
                    
                    # 步骤 3: GitHub 认证
                    self.log("步骤3: 执行 GitHub 认证", "STEP")
                    if 'github.com/login' in page.url or 'github.com/session' in page.url:
                        page.fill('input[name="login"]', username)
                        page.fill('input[name="password"]', account.get("password", ""))
                        page.click('input[type="submit"]')
                        time.sleep(5)
                        if "two-factor" in page.url:
                            totp = pyotp.TOTP(account.get("2fasecret", "").replace(" ", "")).now()
                            page.fill('input[id="app_totp"], input[name="otp"]', totp)
                            page.keyboard.press("Enter")
                            time.sleep(5)
                    
                    # 关键：处理 OAuth 授权页面
                    if 'authorize' in page.url:
                        self.log("检测到 OAuth 授权请求，点击允许...", "INFO")
                        page.click('button[name="authorize"]')
                        time.sleep(5)

                # ====== 步骤4: 等待重定向 (加入严格排除逻辑) ======
                self.log("步骤4: 等待重定向结果", "STEP")
                try:
                    # 循环检查直到 URL 符合要求：包含 claw.cloud 且排除 github/callback
                    success = False
                    for _ in range(12): # 最多等待 60 秒
                        curr_url = page.url
                        if 'claw.cloud' in curr_url and 'github.com' not in curr_url and 'callback' not in curr_url:
                            success = True
                            break
                        self.log(f"等待跳转中... 当前 URL 仍为: {curr_url[:50]}...", "INFO")
                        time.sleep(5)
                    
                    if success:
                        self.log(f"✅ 重定向成功，最终到达 URL: {page.url}", "SUCCESS")
                    else:
                        raise Exception("重定向超时：未能跳转回 Claw 控制台")
                except Exception as e:
                    self.log(f"❌ 重定向失败: {str(e)}", "ERROR")
                    return

                # ====== 步骤5: 验证 (双重过滤) ======
                self.log("步骤5: 验证登录有效性", "STEP")
                final_url = page.url
                
                # 过滤掉非预期页面
                if 'github.com' in final_url or 'callback' in final_url:
                    self.log(f"❌ 验证失败：仍停留在授权或回调页面 ({final_url})", "ERROR")
                    return
                
                if 'claw.cloud' in final_url and 'signin' not in final_url.lower():
                    self.log(f"✅ 验证通过：已成功登录 Claw 系统", "SUCCESS")
                else:
                    self.log(f"❌ 验证失败：URL 状态异常 ({final_url})", "ERROR")
                    return

                # 区域检测
                parsed = urlparse(final_url)
                host = parsed.netloc
                if host.endswith('.console.claw.cloud'):
                    self.detected_region = host.split('.')[0]
                    self.log(f"📍 检测到区域控制台: 【{self.detected_region}】", "SUCCESS")

                # ====== 步骤6: 正在执行保活操作 ======
                self.log("步骤6: 正在执行保活操作...", "STEP")
                
                # 必须基于当前的 Claw 域名访问 dashboard，而不是去访问 github/dashboard
                target_dashboard = f"{parsed.scheme}://{parsed.netloc}/dashboard"
                self.log(f"🔄 访问 Claw 仪表盘: {target_dashboard}")
                page.goto(target_dashboard, wait_until="networkidle", timeout=30000)
                
                try:
                    page.wait_for_selector('text=Console, text=Dashboard, .ant-layout', timeout=15000)
                    self.log("✅ 仪表盘数据加载成功", "SUCCESS")
                except:
                    self.log("⚠️ 仪表盘加载较慢", "WARN")

                # 保存 Cookie (仅首账号)
                if idx == 0:
                    new_s = next((c['value'] for c in context.cookies() if c['name'] == 'user_session'), None)
                    if new_s:
                        self.session_updater.update(new_s)
                        self.log("🔑 Session 已同步", "SUCCESS")

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
            time.sleep(5)

if __name__ == "__main__":
    ClawAutoLogin().run()
