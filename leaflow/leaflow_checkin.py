# leaflow/Leaflow_checkin.py
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

# --- 记录与多维绘图逻辑 ---
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
        masked_name = self._mask(username)
        # 解析数据：假设 balance_info 包含类似 "余额:10, 已用:5, 奖励:0.5" 的信息
        # 如果解析不到则设为 0
        nums = re.findall(r"\d+\.?\d*", str(balance_info))
        
        # 预设逻辑：根据你的 balance_info 输出顺序调整索引
        curr_bal = float(nums[0]) if len(nums) > 0 else 0.0
        used_amt = float(nums[1]) if len(nums) > 1 else 0.0
        # 只有在签到成功且信息中明确含有奖励数值时记录，否则奖励记为 0
        reward = float(nums[2]) if (success and len(nums) > 2) else 0.0
        
        date_str = datetime.now().strftime('%m-%d')
        
        if masked_name not in self.history:
            self.history[masked_name] = []
        
        self.history[masked_name].append({
            "date": date_str,
            "balance": curr_bal,
            "used": used_amt,
            "reward": reward
        })
        
        if len(self.history[masked_name]) > 30:
            self.history[masked_name] = self.history[masked_name][-30:]
        
        with open(self.file_path, 'w', encoding='utf-8') as f:
            json.dump(self.history, f, indent=4)

    def draw_combined_chart(self):
        if not self.history: return
        plt.figure(figsize=(12, 6))
        
        # 线型定义：剩余(实线), 已用(虚线), 奖励(点线)
        styles = {'balance': '-', 'used': '--', 'reward': ':'}
        
        for masked_name, records in self.history.items():
            dates = [r['date'] for r in records]
            # 绘制三条线
            plt.plot(dates, [r['balance'] for r in records], linestyle=styles['balance'], label=f'{masked_name}-Bal')
            plt.plot(dates, [r['used'] for r in records], linestyle=styles['used'], alpha=0.6)
            plt.plot(dates, [r['reward'] for r in records], linestyle=styles['reward'], alpha=0.8)

        plt.title("Combined Accounts Trend (30 Days)")
        plt.xlabel("Date")
        plt.ylabel("Amount")
        plt.legend(bbox_to_anchor=(1.05, 1), loc='upper left', fontsize='small')
        plt.grid(True, linestyle='--', alpha=0.5)
        plt.xticks(rotation=45)
        plt.tight_layout()
        plt.savefig("combined_trend.png")
        plt.close()

history_mgr = HistoryManager()

def run_task_for_account(account, proxy, cookie=None):
    # ... (此处保持你原始代码的 run_task_for_account 逻辑完全不动)
    # 仅在获取 balance_info 后插入：
    success, msg = perform_token_checkin(...)
    balance_info = get_balance_info(page)
    
    history_mgr.record(username, balance_info, success) # 记录数据
    
    print(f"📢 签到结果:{success} ,{msg},{balance_info}")
    return success, final_cookie, f"{note} | {msg},{balance_info}"

# ... (保持 main 逻辑)
def main():
    # ... 执行完所有循环后绘制总图
    # ... 在 get_notifier().send 之前插入：
    history_mgr.draw_combined_chart()
    # ...
