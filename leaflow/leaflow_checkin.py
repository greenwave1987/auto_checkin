import os
import sys
import subprocess
import time
import requests
import json
import re
import hashlib
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

    def _mask(self, name):
        return hashlib.md5(name.encode()).hexdigest()[:8]

    def record(self, username, balance_info, success):
        uid = self._mask(username)
        nums = re.findall(r"\d+\.?\d*", str(balance_info))
        curr_bal = float(nums[0]) if len(nums) > 0 else 0.0
        used_amt = float(nums[1]) if len(nums) > 1 else 0.0
        reward = float(nums[2]) if (success and len(nums) > 2) else 0.0
        
        if uid not in self.history: self.history[uid] = []
        self.history[uid].append({
            "date": datetime.now().strftime('%m-%d'),
            "balance": curr_bal, "used": used_amt, "reward": reward
        })
        if len(self.history[uid]) > 30: self.history[uid] = self.history[uid][-30:]
        with open(self.file_path, 'w', encoding='utf-8') as f:
            json.dump(self.history, f, indent=4)

    def draw(self):
        if not self.history: return
        plt.figure(figsize=(12, 6))
        for uid, records in self.history.items():
            dates = [r.get('date', 'N/A') for r in records]
            bal_vals = [r.get('balance', 0.0) for r in records]
            used_vals = [r.get('used', 0.0) for r in records]
            rew_vals = [r.get('reward', 0.0) for r in records]
            
            # 使用不同的 marker，确保第一天也能看到三个点
            line, = plt.plot(dates, bal_vals, linestyle='-', marker='o', label=f'ID:{uid}-Bal')
            color = line.get_color()
            plt.plot(dates, used_vals, linestyle='--', marker='x', color=color, alpha=0.5, label=f'ID:{uid}-Used')
            plt.plot(dates, rew_vals, linestyle=':', marker='s', color=color, alpha=0.8, label=f'ID:{uid}-Reward')

        plt.title("Accounts Trend (Solid:Balance, Dashed:Used, Dotted:Reward)")
        plt.xlabel("Date")
        plt.ylabel("Amount")
        plt.grid(True, linestyle='--', alpha=0.5)
        plt.xticks(rotation=45)
        # 优化图例，防止重复
        handles, labels = plt.gca().get_legend_handles_labels()
        by_label = dict(zip(labels, handles))
        plt.legend(by_label.values(), by_label.keys(), bbox_to_anchor=(1.05, 1), loc='upper left', fontsize='x-small')
        
        plt.tight_layout()
        plt.savefig("combined_trend.png")
        plt.close()

    def update_readme(self):
        readme_path = "README.md"
        img_tag = "\n\n### 账号数据趋势 (30天)\n![Combined Trend](combined_trend.png)\n"
        content = ""
        if os.path.exists(readme_path):
            with open(readme_path, "r", encoding="utf-8") as f:
                content = f.read()
        if "combined_trend.png" not in content:
            with open(readme_path, "a", encoding="utf-8") as f:
                f.write(img_tag)

history_mgr = HistoryManager()
_notifier = None
config = None

def get_notifier():
    global _notifier, config
    if config is None: config = ConfigReader()
    if _notifier is None: _notifier = TelegramNotifier(config)
    return _notifier
    
def run_task_for_account(account, proxy, cookie=None):
    username = account['username']
    proxy_str = f"{proxy['username']}:{proxy['password']}@{proxy['server']}:{proxy['port']}"
    print(f"\n{'='*40}\n👤 账号: {username}\n{'='*40}")
    gost_proc, pw_bundle, final_cookie = None, None, cookie or ""
    try:
        gost_proc = subprocess.Popen(["./gost", "-L=:8080", f"-F=socks5://{proxy_str}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(5)
        local_proxy = "http://127.0.0.1:8080"
        pw_bundle = open_browser(proxy_url=local_proxy)
        pw, browser, ctx, page = pw_bundle
        if final_cookie:
            page.goto("https://leaflow.net", timeout=30000)
            ctx.add_cookies(final_cookie)
            page.reload()
            if not cookies_ok(page): page = login_and_get_cookies(page, username, account['password'])
        else:
            page = login_and_get_cookies(page, username, account['password'])
        final_cookie = page.context.cookies()
        success, msg = perform_token_checkin(cookies=final_cookie, account_name=username, checkin_url="https://checkin.leaflow.net", main_site="https://leaflow.net", headers={"User-Agent": "Mozilla/5.0"}, proxy_url=local_proxy)
        balance_info = get_balance_info(page)
        history_mgr.record(username, balance_info, success)
        print(f"📢 结果: {success}, {balance_info}")
        return success, final_cookie, f"{msg},{balance_info}"
    except Exception as e:
        print(f"❌ 异常: {e}")
        return False, None, str(e)
    finally:
        if pw_bundle: pw_bundle[1].close(); pw_bundle[0].stop()
        if gost_proc: gost_proc.terminate(); gost_proc.wait()

def main():
    global config
    if config is None: config = ConfigReader()
    newcookies, results = {}, []
    accounts, proxies = config.get_value("LF_INFO"), config.get_value("PROXY_INFO")
    secret = SecretUpdater("LEAFLOW_COOKIES", config_reader=config)
    cookies = secret.load() or {}
    for account, proxy in zip(accounts, proxies):
        ok, n_cookie, msg = run_task_for_account(account, proxy, cookies.get(account['username'],''))
        if ok: newcookies[account['username']] = n_cookie
        results.append(f"{'✅' if ok else '❌'} {account['username']}")
    history_mgr.draw()
    history_mgr.update_readme()
    secret.update(newcookies)
    get_notifier().send(title="Leaflow 签到汇总", content="\n".join(results))

if __name__ == "__main__":
    main()
