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
from engine.leaflow_login import (
    open_browser,
    cookies_ok,
    login_and_get_cookies,
)
from engine.main import (
    perform_token_checkin,
    SecretUpdater,
    getvalue
)


def run_task_for_account(account, proxy):
    """为单个账号启动专属隧道并执行登录签到"""
    username=account['username']
    proxy_str=f"{proxy['username']}:{proxy['password']}@{proxy['server']}:{proxy['port']}"

    print(f"\n{'='*40}")
    print(f"👤 账号: {username}")
    print(f"🌐 代理: {proxy['server']}:{proxy['port']}")
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
        cookies = login_and_get_cookies(page, username, account['password'])

        # 4. 访问面板测试cookie
        if cookies_ok(page):
            print(f"✨ cookies 有效，开始签到！")
        else:
            print(f"✨ cookies 无效，退出！")
            return
        # 5. 执行签到逻辑
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1"
        }
        if cookies:
            success, msg = perform_token_checkin(
                cookies=cookies,
                account_name=username,
                checkin_url="https://checkin.leaflow.net",
                main_site="https://leaflow.net",
                headers=headers,
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
        print(f"✨ 账号 {username} 处理完毕，清理隧道。")

def main():
    useproxy = True

    # 读取账号信息
    accounts = getvalue("LF_INFO")
    
    # 读取代理信息
    proxies = getvalue("PROXY_INFO")

    if not accounts:
        print("❌ 错误: 未配置 LEAFLOW_ACCOUNTS")
        return
    if not proxies:
        print("📢 警告: 未配置 proxy ，将直连")
        useproxy = False

    print(f"📊 检测到 {len(accounts)} 个账号和 {len(proxies)} 个代理")

    # 使用 zip 实现一一对应
    for account, proxy in zip(accounts, proxies):

        run_task_for_account(account, proxy)
        return

if __name__ == "__main__":
    main()
