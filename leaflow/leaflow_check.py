import os
import json
import time
import random
import base64
from typing import Dict
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from engine.main import ConfigReader, SecretUpdater

LOGIN_URL = "https://leaflow.net/login"
DASHBOARD_URL = "https://leaflow.net/dashboard"

# =========================
# Base64 编解码
# =========================
def encode_storage(state: dict) -> str:
    raw = json.dumps(state, ensure_ascii=False)
    return base64.b64encode(raw.encode("utf-8")).decode("utf-8")

def decode_storage(encoded: str) -> dict:
    raw = base64.b64decode(encoded.encode("utf-8")).decode("utf-8")
    return json.loads(raw)

# =========================
# 模拟人类行为
# =========================
def human_fill(page, selector, text):
    el = page.locator(selector).first
    el.wait_for(state="visible", timeout=5000)
    time.sleep(random.uniform(0.4, 1.0))
    el.click()
    for ch in text:
        el.type(ch, delay=random.randint(60, 130))
    time.sleep(random.uniform(0.2, 0.5))

def human_click(page, selector):
    el = page.locator(selector).first
    el.wait_for(state="visible", timeout=5000)
    time.sleep(random.uniform(0.3, 0.8))
    el.hover()
    time.sleep(random.uniform(0.2, 0.4))
    el.click(force=True)

# =========================
# Cookie / Session 校验
# =========================
def cookies_ok(page) -> bool:
    try:
        page.goto(DASHBOARD_URL, timeout=30000)
        page.wait_for_load_state("networkidle")
        return "login" not in page.url.lower()
    except Exception:
        return False

# =========================
# 登录流程
# =========================
def login(page, email, password):
    page.goto(LOGIN_URL, timeout=30000)
    page.wait_for_load_state("domcontentloaded")

    human_fill(page, "#account", email)
    human_fill(page, "#password", password)

    try:
        human_click(page, 'input[type="checkbox"]')
    except Exception:
        pass

    human_click(page, 'button[type="submit"]')
    page.wait_for_load_state("networkidle", timeout=60000)
    time.sleep(3)

    if "login" in page.url.lower():
        raise RuntimeError(f"{email} 登录失败")

# =========================
# 单账号处理
# =========================
def handle_account(p, account, proxy_info, stored_locals: dict) -> dict:
    email = account["username"]
    password = account["password"]

    # Launch 浏览器
    launch_args = {
        "headless": True,
        "args": [
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--exclude-switches=enable-automation",
        ]
    }

    # 可选代理
    if proxy_info:
        proxy_str = f"{proxy_info['server']}:{proxy_info['port']}"
        launch_args["proxy"] = {"server": proxy_str}

    browser = p.chromium.launch(**launch_args)

    # 如果已有 storage 注入
    storage_state = None
    if email in stored_locals:
        storage_state = decode_storage(stored_locals[email].get("storage", "{}"))

    context = browser.new_context(
        storage_state=storage_state,
        viewport={"width": 1920, "height": 1080},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/128.0.0.0 Safari/537.36"
        )
    )

    page = context.new_page()
    page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
        Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN','zh','en'] });
        window.chrome = { runtime: {} };
    """)

    # session 校验
    if storage_state and cookies_ok(page):
        print(f"✨ {email} session 有效")
    else:
        print(f"🔐 {email} 重新登录")
        login(page, email, password)

    # 获取最新 storage
    new_storage = context.storage_state()
    encoded_storage = encode_storage(new_storage)

    browser.close()

    return {
        "email": email,
        "proxy": proxy_info or {},
        "storage": encoded_storage
    }

# =========================
# 主入口
# =========================
def main():
    config = ConfigReader()

    accounts = config.get_value("LF_INFO")
    proxies = config.get_value("PROXY_INFO") or [{}] * len(accounts)

    # SecretUpdater 管理 LEAFLOW_LOCALS
    secret = SecretUpdater("LEAFLOW_LOCALS", config_reader=config)
    stored_locals = secret.load() or {}

    new_locals = {}

    with sync_playwright() as p:
        for account, proxy in zip(accounts, proxies):
            email = account["username"]
            try:
                updated = handle_account(p, account, proxy, stored_locals)
                new_locals[email] = updated
                print(f"✅ {email} 更新成功")
            except Exception as e:
                print(f"❌ {email} 更新失败: {e}")
                if email in stored_locals:
                    new_locals[email] = stored_locals[email]

    # 更新 Secret
    secret.update(new_locals)
    print(f"🎉 所有账号处理完毕，Secret LEAFLOW_LOCALS 已更新")

if __name__ == "__main__":
    main()
