# leaflow/Leaflow_checkin.py
import os
import sys
import subprocess
import time
import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from engine.safe_print import enable_safe_print
enable_safe_print()

from engine.notify import send_notify
from engine.playwright_login import (
    open_browser,
    cookies_ok,
    login_and_get_cookies,
)
from engine.main import (
    perform_token_checkin,
    SecretUpdater,
    getconfig
)

def run_task_for_account(account_str, proxy_str):
    """为单个账号启动专属隧道并执行登录签到"""
    try:
        # 解析账号格式 email----password
        email, password = account_str.split('----')
    except Exception:
        print(f"❌ 账号格式错误 (应为 email----password): {account_str}")
        return

    print(f"\n{'='*40}")
    print(f"👤 账号: {email}")
    print(f"🌐 代理: {proxy_str.split('@')[-1]}")
    print(f"{'='*40}")

    # 1. 启动 Gost 隧道 (将 SOCKS5 转换为本地 8080 HTTP 代理)
    gost_proc = subprocess.Popen(
        ["./gost", "-L=:8080", f"-F=socks5://{proxy_str}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    
    time.sleep(5) # 等待隧道建立
    local_proxy = "http://127.0.0.1:8080"
    pw_bundle = None

    try:
        # 2. 预检代理是否通畅
        res = requests.get("https://api.ipify.org", proxies={"http": local_proxy, "https": local_proxy}, timeout=15)
        print(f"✅ 隧道就绪，出口 IP: {res.text.strip()}")

        # 3. Playwright 登录获取 Cookies
        pw_bundle = open_browser(proxy_url=local_proxy)
        pw, browser, ctx, page = pw_bundle
        cookies = login_and_get_cookies(page, email, password)

        # 4. 执行签到逻辑
        if cookies:
            success, msg = perform_token_checkin(
                cookies=cookies,
                account_name=email,
                checkin_url="https://leaflow.net/user/checkin",
                main_site="https://leaflow.net",
                proxy_url=local_proxy
            )
            print(f"📢 签到结果: {msg}")
        
    except Exception as e:
        print(f"❌ 执行异常: {str(e)}")
    finally:
        # 5. 清理当前账号资源，释放端口供下一个账号使用
        if pw_bundle:
            pw_bundle[1].close() # browser.close()
            pw_bundle[0].stop()  # pw.stop()
        if gost_proc:
            gost_proc.terminate()
            gost_proc.wait()
        print(f"✨ 账号 {email} 处理完毕，清理隧道。")

def main():
    useproxy = True
    password = os.getenv("CONFIG_PASSWORD","").strip()
    if not password:
        raise RuntimeError("❌ 未设置 CONFIG_PASSWORD")
    config = getconfig(password)

    LF_INFO = config.get("LF_INFO","")
    if not LF_INFO:
        raise RuntimeError("❌ 配置文件中不存在 LF_INFO")
    print(f'ℹ️ 已读取: {LF_INFO.get("description","")}')

    accounts = LF_INFO.get("value","")
    # 读取 Secrets 环境变量
    raw_accounts = os.getenv("LEAFLOW_ACCOUNTS", "").strip()
    raw_proxies = os.getenv("SOCKS5_INFO", "").strip()

    accounts = [a.strip() for a in raw_accounts.split('\n') if a.strip()]
    proxies = [p.strip() for p in raw_proxies.split(',') if p.strip()]

    if not accounts:
        print("❌ 错误: 未配置 LEAFLOW_ACCOUNTS")
        return

    print(f"📊 检测到 {len(accounts)} 个账号和 {len(proxies)} 个代理")

    # 使用 zip 实现一一对应
    for account, proxy in zip(accounts, proxies):
        run_task_for_account(account, proxy)

if __name__ == "__main__":
    main()
