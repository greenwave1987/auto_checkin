import os, sys, subprocess, time, requests, json, re, hashlib
import matplotlib.pyplot as plt
import numpy as np
from datetime import datetime

# 强制使用 Agg 后端并配置字体
import matplotlib
matplotlib.use('Agg')
plt.rcParams['font.sans-serif'] = ['DejaVu Sans'] 
plt.rcParams['axes.unicode_minus'] = False 

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from engine.safe_print import enable_safe_print
enable_safe_print()

from engine.notify import TelegramNotifier
from engine.leaflow_login import open_browser, cookies_ok, login_and_get_cookies, get_balance_info
from engine.main import perform_token_checkin, SecretUpdater, ConfigReader

class HistoryManager:
    def __init__(self, file_path="checkin_history.json"):
        self.file_path = file_path
        self.history = self._load()

    def _load(self):
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    print(f"✅ 成功加载历史记录，共 {len(data)} 个账号数据")
                    return data
            except Exception as e:
                print(f"⚠️ 历史记录加载失败: {e}")
                return {}
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
        print(f"📝 已更新 ID:{uid} 的历史轨迹 (Bal:{curr_bal}, Used:{used_amt}, Rew:{reward})")

    def draw(self):
        if not self.history: return
        print("🎨 正在生成 leaflow金额曲线图...")
        plt.figure(figsize=(12, 6))
        for i, (uid, records) in enumerate(self.history.items()):
            dates = [r.get('date', 'N/A') for r in records]
            bal_vals = np.array([r.get('balance', 0.0) for r in records])
            used_vals = np.array([r.get('used', 0.0) for r in records])
            rew_vals = np.array([r.get('reward', 0.0) for r in records])
            
            # 坐标微调，防止多个 0 点重合
            offset = i * 0.15 
            line, = plt.plot(dates, bal_vals, linestyle='-', marker='o', markersize=6, label=f'ID:{uid}-余额')
            color = line.get_color()
            plt.plot(dates, used_vals + offset, linestyle='--', marker='x', markersize=7, color=color, alpha=0.5, label=f'ID:{uid}-已用')
            plt.plot(dates, rew_vals + (offset * 2), linestyle=':', marker='s', markersize=5, color=color, alpha=0.8, label=f'ID:{uid}-奖励')

        plt.title("leaflow金额曲线图")
        plt.xlabel("日期")
        plt.ylabel("数值 (多账号 0 点已偏移)")
        plt.grid(True, linestyle='--', alpha=0.5)
        plt.xticks(rotation=45)
        
        handles, labels = plt.gca().get_legend_handles_labels()
        by_label = dict(zip(labels, handles))
        plt.legend(by_label.values(), by_label.keys(), bbox_to_anchor=(1.05, 1), loc='upper left', fontsize='x-small')
        
        plt.tight_layout()
        plt.savefig("combined_trend.png")
        plt.close()
        print("🖼️ 图表渲染完成并保存为 combined_trend.png")

    def update_readme(self):
        readme_path = "README.md"
        img_tag = "\n\n### leaflow金额曲线图\n![Combined Trend](combined_trend.png)\n"
        content = ""
        if os.path.exists(readme_path):
            with open(readme_path, "r", encoding="utf-8") as f:
                content = f.read()
        if "combined_trend.png" not in content:
            with open(readme_path, "a", encoding="utf-8") as f:
                f.write(img_tag)
            print("📝 已在 README.md 追加图表引用")

history_mgr = HistoryManager()

def mask_email(email):
    if "@" not in email: return email
    prefix, domain = email.split("@")
    return f"{prefix[:3]}***{prefix[-2:]}@{domain}" if len(prefix) > 5 else f"{prefix[0]}***@{domain}"

def run_task_for_account(account, proxy, cookie=None):
    username = account['username']
    m_user = mask_email(username)
    proxy_str = f"{proxy['username']}:{proxy['password']}@{proxy['server']}:{proxy['port']}"
    
    print(f"\n{'='*50}")
    print(f"👤 开始处理账号: {username}")
    print(f"🌐 使用代理: {proxy['server']}:{proxy['port']}")
    print(f"{'='*50}")
    
    gost_proc, pw_bundle, final_cookie = None, None, cookie or ""
    try:
        # 建立代理隧道
        gost_proc = subprocess.Popen(["./gost", "-L=:8080", f"-F=socks5://{proxy_str}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(5)
        local_proxy = "http://127.0.0.1:8080"
        
        # IP 检测日志
        ip_res = requests.get("https://api.ipify.org", proxies={"http": local_proxy, "https": local_proxy}, timeout=15)
        print(f"🌍 出口 IP 确认: {ip_res.text.strip()}")
        
        pw_bundle = open_browser(proxy_url=local_proxy)
        pw, browser, ctx, page = pw_bundle
        
        status_note = "新登录"
        if final_cookie:
            print("🍪 检测到现有 Cookie，尝试注入...")
            page.goto("https://leaflow.net", timeout=30000)
            ctx.add_cookies(final_cookie)
            page.reload()
            if cookies_ok(page): 
                print("✨ Cookie 有效，跳过登录步骤")
                status_note = "cookie 有效，无需登录"
            else: 
                print("🔄 Cookie 过期，开始重新登录...")
                page = login_and_get_cookies(page, username, account['password'])
        else:
            print("🔑 无 Cookie 记录，开始初次登录...")
            page = login_and_get_cookies(page, username, account['password'])
            
        final_cookie = page.context.cookies()
        
        print("🎯 开始执行签到请求...")
        success, msg = perform_token_checkin(cookies=final_cookie, account_name=username, checkin_url="https://checkin.leaflow.net", main_site="https://leaflow.net", headers={"User-Agent": "Mozilla/5.0"}, proxy_url=local_proxy)
        
        balance_info = get_balance_info(page)
        print(f"📊 账户快照: {balance_info}")
        
        history_mgr.record(username, balance_info, success)
        
        # 详细通知日志
        detail = f" 账号：{m_user}\n    成功: {status_note} | {msg},{balance_info}"
        return success, final_cookie, detail
        
    except Exception as e:
        err_msg = f" 账号：{m_user}\n    失败: {str(e)}"
        print(f"❌ 处理出错: {e}")
        return False, None, err_msg
    finally:
        if pw_bundle: pw_bundle[1].close(); pw_bundle[0].stop()
        if gost_proc: gost_proc.terminate(); gost_proc.wait()
        print(f"🏁 账号 {username} 处理流程结束")

def main():
    print("🚀 Leaflow 自动化脚本开始运行...")
    config = ConfigReader()
    newcookies, results = {}, []
    accounts, proxies = config.get_value("LF_INFO"), config.get_value("PROXY_INFO")
    secret = SecretUpdater("LEAFLOW_COOKIES", config_reader=config)
    cookies = secret.load() or {}
    
    if not accounts:
        print("🛑 配置文件中未找到账号，请检查 LF_INFO")
        return

    for account, proxy in zip(accounts, proxies):
        ok, n_cookie, detail_msg = run_task_for_account(account, proxy, cookies.get(account['username'],''))
        if ok: newcookies[account['username']] = n_cookie
        results.append(detail_msg)

    print("\n📈 任务统计与绘图阶段...")
    history_mgr.draw()
    history_mgr.update_readme()
    
    print("🔒 正在同步 Cookie 到环境变量...")
    secret.update(newcookies)
    
    print("📤 发送 Telegram 汇总通知...")
    notifier = TelegramNotifier(config)
    notifier.send(title="Leaflow 自动签到汇总", content="\n".join(results))
    print("🌟 所有任务已圆满完成")

if __name__ == "__main__":
    main()
