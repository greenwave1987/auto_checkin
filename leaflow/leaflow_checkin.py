# leaflow/Leaflow_checkin.py
import os
import sys
import subprocess
import time
import requests
import json
import re
import matplotlib.pyplot as plt
from datetime import datetime

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

# --- 仅增加记录相关逻辑，不触动原逻辑 ---
class HistoryManager:
    def __init__(self, file_path="checkin_history.json"):
        self.file_path = file_path
        self.history = self._load()

    def _load(self):
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except: return {}
        return {}

    def record(self, username, balance_info):
        # 提取余额中的数字
        nums = re.findall(r"\d+\.?\d*", str(balance_info))
        current_balance = float(nums[0]) if nums else 0.0
        date_str = datetime.now().strftime('%m-%d')
        
        if username not in self.history:
            self.history[username] = []
        
        self.history[username].append({"date": date_str, "balance": current_balance})
        # 保持30天并自动替换旧的
        if len(self.history[username]) > 30:
            self.history[username] = self.history[username][-30:]
        
        with open(self.file_path, 'w', encoding='utf-8') as f:
            json.dump(self.history, f, indent=4)
        self._draw(username)

    def _draw(self, username):
        data = self.history.get(username, [])
        if not data: return
        dates = [d['date'] for d in data]
        balances = [d['balance'] for d in data]
        plt.figure(figsize=(10, 5))
        plt.plot(dates, balances, marker='o', linestyle='-', color='#007bff')
        plt.title(f"30-Day Trend: {username}")
        plt.grid(True, linestyle='--', alpha=0.6)
        plt.xticks(rotation=45)
        plt.tight_layout()
        plt.savefig(f"trend_{username}.png")
        plt.close()

history_mgr = HistoryManager()
# ------------------------------------

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
        # 1️⃣ 启动 Gost 隧道
        gost_proc = subprocess.Popen(
            ["./gost", "-L=:8080", f"-F=socks5://{proxy_str}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        time.sleep(5)
        local_proxy = "http://127.0.0.1:8080"

        # 2️⃣ 测试隧道
        res = requests.get("https://api.ipify.org", proxies={"http": local_proxy, "https": local_proxy}, timeout=15)
        print(f"✅ 隧道就绪，出口 IP: {res.text.strip()}")

        # 3️⃣ 打开浏览器
        pw_bundle = open_browser(proxy_url=local_proxy)
        pw, browser, ctx, page = pw_bundle

        # 4️⃣ Cookie 处理
        if final_cookie:
            print("🔹 注入已有 cookie 测试有效性")
            page.goto("https://leaflow.net", timeout=30000)
            ctx.add_cookies(final_cookie)
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
        
        # 5️⃣ 执行签到
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
        
        # --- 仅在此处增加记录逻辑，不改动 print ---
        if success:
            history_mgr.record(username, balance_info)

        print(f"📢 签到结果:{success} ,{msg},{balance_info}")
        return success, final_cookie, f"{note} | {msg},{balance_info}"

    except Exception as e:
        print(f"❌ 账号 {username} 执行异常: {e}")
        return False,  None, f"❌ 执行异常: {e}"

    finally:
        if pw_bundle:
            pw_bundle[1].close()
            pw_bundle[0].stop()
        if gost_proc:
            gost_proc.terminate()
            gost_proc.wait()
        print(f"✨ 账号 {username} 处理完毕，清理隧道。")

# jrun_task_for_account 保持原样不做改动... (由于你未在主流程调用它，此处略过)

def main():
    global config
    if config is None:
        config = ConfigReader()
    useproxy = True
    newcookies={}
    results = []

    accounts = config.get_value("LF_INFO")
    proxies = config.get_value("PROXY_INFO")
    secret = SecretUpdater("LEAFLOW_COOKIES", config_reader=config)
    cookies = secret.load() or {}

    if not accounts:
        print("❌ 错误: 未配置 LEAFLOW_ACCOUNTS")
        return
    if not proxies:
        print("📢 警告: 未配置 proxy ，将直连")
        useproxy = False

    print(f"📊 检测到 {len(accounts)} 个账号和 {len(proxies)} 个代理")

    for account, proxy in zip(accounts, proxies):
        username=account['username']
        print(f"🚀 开始处理账号: {username}, 使用代理: {proxy['server']}")
        results.append(f"🚀 账号：{username}, 使用代理: {proxy['server']}")
        try:
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

    secret.update(newcookies)
    get_notifier().send(
        title="Leaflow 自动签到汇总",
        content="\n".join(results)
    )

if __name__ == "__main__":
    main()
