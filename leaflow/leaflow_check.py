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
    
def run_task_for_account(account, proxy, storage_data=None):
    """
    为单个账号启动专属隧道并执行登录签到
    - account: dict, 至少包含 'username' 和 'password'
    - proxy: dict, 至少包含 'server','port','username','password'
    - storage_data: 可选已有 storage_state (dict)
    返回:
        ok: bool, 是否签到成功
        new_storage: dict, 状态字典
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
    final_storage = storage_data

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
        # 注意：这里如果 open_browser 支持传入 storage_state 最好，
        # 如果不支持，我们通过 ctx 注入
        pw_bundle = open_browser(proxy_url=local_proxy)
        pw, browser, ctx, page = pw_bundle

        # ----------------------------
        # 4️⃣ 如果已有 storage_state，先注入测试
        # ----------------------------
        if final_storage:
            print("🔹 注入已有 storage_state 测试有效性")
            # 重新创建一个带有 storage_state 的 context 是最稳妥的，
            # 但为了保持原代码结构，我们直接跳转并观察。
            # Playwright 无法直接给已运行的 ctx "追填" storage_state，
            # 这里逻辑上通常是读取 cookies 并注入，或者 open_browser 时传入。
            # 假设 context 已经建立，我们注入其中的 cookies 部分：
            if 'cookies' in final_storage:
                ctx.add_cookies(final_storage['cookies'])
            
      
            if cookies_ok(page):
                print(f"✨ storage 有效，无需登录")
                note = f"✨ storage 有效，无需登录"
            else:
                print(f"⚠ storage 无效，需要登录获取")
                note = f"⚠ storage 无效，需要登录获取"
                page = login_and_get_cookies(page, username, account['password'])
        else:
            print("⚠ 没有保存的状态，开始登录获取")
            note = f"⚠ 没有保存的状态，开始登录获取"
            page = login_and_get_cookies(page, username, account['password'])
        
        # 获取最新的完整状态
        final_storage = page.context.storage_state()
        
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

        # 签到接口通常只需要 cookies
        success, msg = perform_token_checkin(
            cookies=final_storage.get('cookies', []),
            account_name=username,
            checkin_url="https://checkin.leaflow.net",
            main_site="https://leaflow.net",
            headers=headers,
            proxy_url=local_proxy
        )
        balance_info=get_balance_info(page)
        print(f"📢 签到结果:{success} ,{msg},{balance_info}")

        return success, final_storage, f"{note} | {msg},{balance_info}"

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

def main():
    global config
    if config is None:
        config = ConfigReader()
    useproxy = True
    new_storages={}
    results = []

    # 读取账号信息
    accounts = config.get_value("LF_INFO")
    
    # 读取代理信息
    proxies = config.get_value("PROXY_INFO")

    # 修改 Secret 名为 LEAFLOW_STORAGE
    secret = SecretUpdater("LEAFLOW_STORAGE", config_reader=config)

    # 读取已保存的 storage_state
    all_storages = secret.load() or {}

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
            # 执行任务，传入对应的 storage_state
            ok, current_storage, msg = run_task_for_account(account, proxy, all_storages.get(username))
    
            if ok:
                print(f"    ✅ 执行成功，更新 storage_state")
                results.append(f"    ✅ 执行成功:{msg}")
                new_storages[username] = current_storage
            else:
                print(f"    ⚠️ 执行失败，不保存更新")
                results.append(f"    ⚠️ 执行失败:{msg}")
                # 失败时可以选择保留旧的，或者不保存。此处遵循原逻辑：不保存新状态
    
        except Exception as e:
            print(f"    ❌ 执行异常: {e}")
            results.append(f"    ❌ 执行异常: {e}")
        return
    # 写入更新后的所有账号状态
    secret.update(new_storages)
    # 发送结果
    get_notifier().send(
        title="Leaflow 自动签到汇总",
        content="\n".join(results)
    )

if __name__ == "__main__":
    main()
