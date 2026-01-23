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

# 设置支持中文的字体（GitHub Actions 环境通常需此设置或保持默认）
plt.rcParams['font.sans-serif'] = ['SimHei', 'DejaVu Sans'] 
plt.rcParams['axes.unicode_minus'] = False 

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
            
            # 余额：实线+圆点
            line, = plt.plot(dates, bal_vals, linestyle='-', marker='o', label=f'ID:{uid}-余额')
            color = line.get_color()
            # 已用：虚线+叉号
            plt.plot(dates, used_vals, linestyle='--', marker='x', color=color, alpha=0.5, label=f'ID:{uid}-已用')
            # 奖励：点线+方块
            plt.plot(dates, rew_vals, linestyle=':', marker='s', color=color, alpha=0.8, label=f'ID:{uid}-奖励')

        plt.title("leaflow金额曲线图")
        plt.xlabel("日期")
        plt.ylabel("金额")
        plt.grid(True, linestyle='--', alpha=0.5)
        plt.xticks(rotation=45)
        
        # 优化图例
        handles, labels = plt.gca().get_legend_handles_labels()
        by_label = dict(zip(labels, handles))
        plt.legend(by_label.values(), by_label.keys(), bbox_to_anchor=(1.05, 1), loc='upper left', fontsize='x-small')
        
        plt.tight_layout()
        plt.savefig("combined_trend.png")
        plt.close()

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

history_mgr = HistoryManager()
# ... 保持 run_task_for_account 和 main 函数逻辑与上一个版本一致 ...
