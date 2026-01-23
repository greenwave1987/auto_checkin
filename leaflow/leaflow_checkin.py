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

from engine.notify import TelegramNotifier
from engine.leaflow_login import (
    open_browser,
    cookies_ok,
    login_and_get_cookies,
    get_balance_info
)
from engine.main import (
    perform_token_checkin,
    SecretUpdater,
    ConfigReader
)

# 初始化
_notifier = None
config = None

def get_notifier():
    global _notifier,config
    if config is None:
        config = ConfigReader()
    if _notifier is None:
        _notifier = TelegramNotifier(config)
    return _notifier
    
def run_task_for_account(account, proxy, cookie=None):
    """
    为单个账号启动专属隧道并执行登录签到
    - account: dict, 至少包含 'username' 和 'password'
    - proxy: dict, 至少包含 'server','port','username','password'
    - cookie: 可选已有 cookie
    返回:
        ok: bool, 是否签到成功
        newcookie: dict, {username: cookie}，用于更新统一 cookie 字典
    """
    note = ""
    username = account['username']
    proxy_str = f"{proxy['username']}:{proxy['password']}@{proxy['server']}:{proxy['port']}"
    
    print(f"\n{'='*40}")
    print(f"👤 账号: {username}")
    print(f"🌐 代理: {proxy['server']}:{proxy['port']}")
    print(f"{'='*40}")

    gost_proc = None
    pw_bundle = None
    final_cookie = cookie or ""

    try:
        # ----------------------------
        # 1️⃣ 启动 Gost 隧道
        # ----------------------------
        gost_proc = subprocess.Popen(
            ["./gost", "-L=:8080", f"-F=socks5://{proxy_str}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        time.sleep(5)
        local_proxy = "http://127.0.0.1:8080"

        # ----------------------------
        # 2️⃣ 测试隧道是否可用
        # ----------------------------
        res = requests.get("https://api.ipify.org", proxies={"http": local_proxy, "https": local_proxy}, timeout=15)
        print(f"✅ 隧道就绪，出口 IP: {res.text.strip()}")

        # ----------------------------
        # 3️⃣ 打开浏览器
        # ----------------------------
        pw_bundle = open_browser(proxy_url=local_proxy)
        pw, browser, ctx, page = pw_bundle

        # ----------------------------
        # 4️⃣ 如果已有 cookie，先注入测试
        # ----------------------------
        if final_cookie:
            print("🔹 注入已有 cookie 测试有效性")
            page.goto("https://leaflow.net", timeout=30000)
            ctx.add_cookies(final_cookie)  # 直接传 login_and_get_cookies 返回的列表
            page.reload()
        
            if cookies_ok(page):
                print(f"✨ cookie 有效，无需登录")
                note = f"✨ cookie 有效，无需登录"
            else:
                print(f"⚠ cookie 无效，需要登录获取")
                note = f"⚠ cookie 无效，需要登录获取"
                page = login_and_get_cookies(page, username, account['password'])
        else:
            print("⚠ 没有 cookie，开始登录获取")
            note = f"⚠ 没有 cookie，开始登录获取"
            page = login_and_get_cookies(page, username, account['password'])
        
        final_cookie=page.context.cookies()
        
        # ----------------------------
        # 5️⃣ 执行签到逻辑
        # ----------------------------
        print("📝 开始签到")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1"
        }

        success, msg = perform_token_checkin(
            cookies=final_cookie,
            account_name=username,
            checkin_url="https://checkin.leaflow.net",
            main_site="https://leaflow.net",
            headers=headers,
            proxy_url=local_proxy
        )
        balance_info=get_balance_info(page)
        print(f"📢 签到结果:{success} ,{msg},{balance_info}")

        return success, final_cookie, f"{note} | {msg},{balance_info}"

    except Exception as e:
        print(f"❌ 账号 {username} 执行异常: {e}")
        return False,  None, f"❌ 执行异常: {e}"

    finally:
        # ----------------------------
        # 6️⃣ 清理资源
        # ----------------------------
        if pw_bundle:
            pw_bundle[1].close()  # browser.close()
            pw_bundle[0].stop()   # pw.stop()
        if gost_proc:
            gost_proc.terminate()
            gost_proc.wait()
        print(f"✨ 账号 {username} 处理完毕，清理隧道。")
def jrun_task_for_account(account, proxy,cookie=None):
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
    global config
    if config is None:
        config = ConfigReader()
    useproxy = True
    newcookies={}
    results = []

    # 读取账号信息
    accounts = config.get_value("LF_INFO")
    
    # 读取代理信息
    proxies = config.get_value("PROXY_INFO")

    # 初始化 SecretUpdater，会自动根据当前仓库用户名获取 token
    secret = SecretUpdater("LEAFLOW_COOKIES", config_reader=config)

    # 读取
    cookies = secret.load() or {}

    if not accounts:
        print("❌ 错误: 未配置 LEAFLOW_ACCOUNTS")
        return
    if not proxies:
        print("📢 警告: 未配置 proxy ，将直连")
        useproxy = False

    print(f"📊 检测到 {len(accounts)} 个账号和 {len(proxies)} 个代理")

    # 使用 zip 实现一一对应
    for account, proxy in zip(accounts, proxies):
        username=account['username']

        print(f"🚀 开始处理账号: {username}, 使用代理: {proxy['server']}")
        results.append(f"🚀 账号：{username}, 使用代理: {proxy['server']}")
        try:
            # run_task_for_account 返回 ok（bool）和 newcookie（dict 或 str）
            ok, newcookie,msg = run_task_for_account(account, proxy,cookies.get(username,''))
    
            if ok:
                print(f"    ✅ 执行成功，保存新 cookie")
                results.append(f"    ✅ 执行成功:{msg}")
                newcookies[username]=newcookie
            else:
                print(f"    ⚠️ 执行失败，不保存 cookie")
                results.append(f"    ⚠️ 执行失败:{msg}")
    
        except Exception as e:
            print(f"    ❌ 执行异常: {e}")
            results.append(f"    ❌ 执行异常: {e}")

    # 写入
    secret.update(newcookies)
    # 发送结果
    get_notifier().send(
        title="Leaflow 自动签到汇总",
        content="\n".join(results)
    )

if __name__ == "__main__":
    main()
