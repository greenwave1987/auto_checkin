#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import requests
from seleniumbase import SB

# ============================================================
#  基础配置
# ============================================================
LOGIN_URL = "https://freecloud.ltd/auth/login"
DASHBOARD_URL = "https://freecloud.ltd/user"

EMAIL = os.environ.get("FC_EMAIL")
PASSWORD = os.environ.get("FC_PASSWORD")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID")

def send_tg_message(status_icon, status_text, detail=""):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("ℹ️ 未配置 TG 推送，跳过。")
        return
    text = f"{status_icon} **Freecloud 通知**\n状态: {status_text}\n详情: {detail}"
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        print(f"  ⚠️ TG 发送失败: {e}")

# ============================================================
#  核心逻辑
# ============================================================

def handle_turnstile(sb):
    """专门处理 Cloudflare Turnstile 验证"""
    print("🛡️ 正在检测 Cloudflare 验证状态...")
    time.sleep(3)
    
    # 检查是否存在验证码 iframe
    if sb.is_element_visible('iframe[src*="challenges"]'):
        print("🖱️ 发现 Turnstile 验证框，尝试模拟点击...")
        try:
            # SeleniumBase 核心魔法：自动定位并点击验证码复选框
            sb.driver.uc_gui_click_captcha()
            print("⏳ 已发送点击指令，等待验证通过...")
            time.sleep(5)
        except Exception as e:
            print(f"⚠️ 模拟点击异常 (可能是无头模式限制): {e}")
    else:
        print("✅ 未发现明显的验证码拦截。")

def login(sb):
    """执行登录流程"""
    print(f"🌐 访问地址: {LOGIN_URL}")
    # uc_open_with_reconnect 能有效绕过初级屏蔽
    sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=5)
    
    # 第一步：解决可能存在的验证码
    handle_turnstile(sb)

    # 第二步：等待并填写表单
    print("⏳ 正在定位登录表单...")
    try:
        # 兼容多种可能的选择器
        email_selector = 'input[type="email"], input[name="email"], input[placeholder*="邮箱"]'
        sb.wait_for_element(email_selector, timeout=20)
        
        print("📧 填写账号密码...")
        sb.type(email_selector, EMAIL)
        time.sleep(0.5)
        sb.type('input[type="password"]', PASSWORD)
        time.sleep(1)
        
        print("🖱️ 提交登录...")
        # 优先点击按钮，如果找不到则回车
        if sb.is_element_visible('button[type="submit"]'):
            sb.click('button[type="submit"]')
        else:
            sb.press_keys('input[type="password"]', '\n')
            
    except Exception as e:
        print(f"❌ 登录表单加载失败: {e}")
        sb.save_screenshot("login_timeout.png")
        return False

    # 第三步：判断登录结果
    print("⏳ 等待页面跳转...")
    for _ in range(10):
        time.sleep(1.5)
        if "/user" in sb.get_current_url() or "/dashboard" in sb.get_current_url():
            print("✅ 成功进入用户中心")
            return True
    
    print("❌ 登录失败，页面未跳转。")
    sb.save_screenshot("login_failed.png")
    return False

def check_in(sb):
    """执行签到流程"""
    print(f"🚀 进入签到页面: {DASHBOARD_URL}")
    sb.open(DASHBOARD_URL)
    time.sleep(5)

    try:
        # V2Board 常见的签到文案
        btns = ["每日签到", "点我签到", "立即签到"]
        for btn_text in btns:
            if sb.is_text_visible(btn_text):
                print(f"🖱️ 发现签到按钮: {btn_text}")
                sb.click(f'button:contains("{btn_text}")')
                time.sleep(3)
                
                # 尝试获取签到成功后的弹窗消息
                try:
                    msg = sb.get_text('.v-toast__text')
                except:
                    msg = "签到动作已完成"
                
                print(f"🎉 结果: {msg}")
                send_tg_message("✅", "签到成功", msg)
                return True
        
        if sb.is_text_visible("已签到"):
            print("😊 今天已经签过到啦！")
            send_tg_message("✅", "已签到", "今日任务已完成，无需重复操作")
            return True
            
        print("⚠️ 未发现签到按钮，可能页面结构已改变。")
        send_tg_message("⚠️", "未找到签到按钮", "请检查截图确认页面布局")
        sb.save_screenshot("checkin_not_found.png")
        
    except Exception as e:
        print(f"❌ 签到过程发生错误: {e}")
        send_tg_message("❌", "签到异常", str(e))

# ============================================================
#  主入口
# ============================================================
def main():
    if not EMAIL or not PASSWORD:
        print("❌ 致命错误：未配置环境变量 FC_EMAIL 或 FC_PASSWORD")
        return

    # 配置启动参数
    # 在 GitHub Actions 运行时建议 headless=True，但如果验证过不去，可尝试 False
    sb_kwargs = {
        "uc": True,             # 必须开启 UC 模式
        "test": True,           # 增强规避特征
        "headless": True,       # 生产环境建议 True
        "browser": "chrome",
        "locale_code": "zh-CN"
    }

    with SB(**sb_kwargs) as sb:
        try:
            if login(sb):
                check_in(sb)
            else:
                send_tg_message("❌", "登录环节失败", "通常是因为 Cloudflare 验证未通过或账号错误")
        except Exception as e:
            print(f"🔥 运行崩溃: {e}")
            send_tg_message("🔥", "脚本崩溃", str(e))

if __name__ == "__main__":
    main()
