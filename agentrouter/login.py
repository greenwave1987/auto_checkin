import os
import re
import sys
import json
import time
import base64
import random
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

# ==================== 基准数据对接 ====================
BASE_URL = "https://agentrouter.org"
TIMEOUT = 15
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from engine.notify import TelegramNotifier
try:
    from engine.main import ConfigReader, SecretUpdater, test_proxy
except ImportError:
    class ConfigReader:
        def get_value(self, key): return os.environ.get(key)
    class SecretUpdater:
        def __init__(self, name=None, config_reader=None): pass
        def update(self, value): return False

# ==================== 配置 ====================
PROXY_DSN = os.environ.get("PROXY_DSN", "").strip()
BOARD_ENTRY_URL = "https://agentrouter.org/login"

_notifier = None
config = None

def get_notifier():
    global _notifier, config
    if config is None:
        config = ConfigReader()
    if _notifier is None:
        _notifier = TelegramNotifier(config)
    return _notifier

# ==================== 工具函数 ====================
def mask_name(name: str):
    return f"{name[:2]}***{name[-2:]}"

import json

def slim_storage_state(state):
    """精简 storage_state，只保留核心登录凭据"""
    if not isinstance(state, dict):
        return state

    # 1. 过滤 Cookies
    if "cookies" in state:
        state["cookies"] = [
            c for c in state["cookies"] 
            if "agentrouter.org" in c.get("domain", "")
        ]

    # 2. 过滤 localStorage (origins)
    if "origins" in state:
        new_origins = []
        essential_keys = ["session", "user"]
        
        for o in state["origins"]:
            # 关键修复：Playwright 字段名是 origin (如 "https://agentrouter.org")
            target_origin = o.get("origin", "")
            if "agentrouter.org" in target_origin:
                storage = o.get("localStorage", [])
                slim_storage = [
                    item for item in storage 
                    if item.get("name") in essential_keys
                ]
                o["localStorage"] = slim_storage
                new_origins.append(o)
        
        state["origins"] = new_origins

    #print(json.dumps(state, indent=2, ensure_ascii=False))
    return state

class AutoLogin:
    """自动登录，注入 GH_SESSION 获取并同步 agentrouter LocalStorage"""
    
    def __init__(self, config):
        self.host = urlparse(BOARD_ENTRY_URL).netloc
        self.gh_username = config.get('gh_username')
        
        gh_sess = config.get('gh_session', '')
        if isinstance(gh_sess, str):
            self.gh_session = gh_sess.strip()
        elif isinstance(gh_sess, list):
            self.gh_session = gh_sess[0] if gh_sess else ''
        else:
            self.gh_session = ''
        
        ag_local_val = config.get('ag_local', '')
        if isinstance(ag_local_val, str):
            self.ag_local = ag_local_val.strip()
        else:
            self.ag_local = ag_local_val
        
        self.ag_proxy = config.get('ag_proxy')
        self.proxy_url = test_proxy(self.ag_proxy) if self.ag_proxy else None
        if not self.proxy_url:
            self.ag_proxy = config.get('wz_proxy')
            self.proxy_url = test_proxy(self.ag_proxy) if self.ag_proxy else None
            
        self.notify = config.get('notify')
        self.shots = []
        self.logs = []
        self.n = 0
        self.region_base_url = 'https://ap-northeast-1.run.agentrouter.org'
        
    def log(self, msg, level="INFO"):
        icons = {"INFO": "ℹ️", "SUCCESS": "✅", "ERROR": "❌", "WARN": "⚠️", "STEP": "🔹"}
        line = f"{icons.get(level, '•')} {msg}"
        print(line, flush=True)
        self.logs.append(line)
    
    def shot(self, page, name):
        self.n += 1
        f = f"{self.n:02d}_{name}.png"
        try:
            page.screenshot(path=f)
            self.shots.append(f)
        except Exception:
            pass
        return f
    
    def click(self, page, desc=""):
        """增强版点击：兼容主页跳转、Popup弹窗与SPA异步路由"""
        self.log(f"🔍 尝试查找并点击: {desc}", "INFO")
        
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1000)
        except Exception:
            pass
    
        frames = page.frames
        selectors = [
            'button:has([aria-label="github_logo"])',
            'button:has-text("Continue with GitHub")',
            'button:has-text("GitHub")',
            'a[href="/auth/login/github"]',
            'a[href*="/auth/login/github"]',
            '[data-provider="github"]',
            '//button[contains(.,"Continue with GitHub")]'
        ]
    
        for frame in frames:
            for sel in selectors:
                try:
                    el = frame.locator(sel).first
                    if not el.is_visible(timeout=2000):
                        continue
                        
                    self.log(f"找到按钮: {sel}", "SUCCESS")
                    time.sleep(random.uniform(0.3, 0.6))
                    
                    try:
                        el.hover()
                    except Exception:
                        pass
    
                    # 同时监听 Popup 弹窗与页面导航事件
                    with page.expect_popup(timeout=5000) as popup_info:
                        try:
                            # 使用 Playwright 原生物理点击（移除强行 expect_navigation 避免误触发超时）
                            el.click(force=True, timeout=3000)
                        except Exception:
                            # 物理点击失败时降级使用 JS dispatchEvent
                            el.dispatch_event('click')
                    
                    # 场景 1：成功捕获到 Popup 新窗口
                    popup_page = popup_info.value
                    self.log(f"✅ 检测到 OAuth 弹窗: {popup_page.url}", "SUCCESS")
                    
                    # 等待弹窗自动完成重定向（例如 GitHub 授权后自动关闭或跳转返回）
                    try:
                        popup_page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        pass
                    return True
    
                except PlaywrightTimeoutError:
                    # 场景 2：未触发 Popup，检查主页面是否发生 URL 变更或导航
                    try:
                        page.wait_for_url(lambda u: "login" not in u.lower(), timeout=5000)
                        self.log(f"✅ 主页面 URL 已变更: {page.url}", "SUCCESS")
                        return True
                    except PlaywrightTimeoutError:
                        # 场景 3：DOM 事件后触发的延迟跳转/刷新
                        time.sleep(2)
                        if "login" not in page.url.lower():
                            self.log(f"✅ 事件触发成功，当前 URL: {page.url}", "SUCCESS")
                            return True
                            
                except Exception as e:
                    self.log(f"{sel} 点击流程异常: {e}", "DEBUG")
                    continue
    
        self.log(f"❌ 找不到或无法激活按钮: {desc}", "ERROR")
        return False

    def get_balance_with_token(self, page):
        """从 localStorage 读取 user.id 并执行 /api/user/self 接口进行保活"""
        self.log("开始执行保活请求 (/api/user/self)...", "STEP")
        return_msg = ""
        
        try:
            result_data = page.evaluate("""
                async () => {
                    try {
                        // 1. 从 localStorage 中读取 user 信息并解析 ID
                        let apiUserId = "";
                        const rawUser = localStorage.getItem("user");
                        if (rawUser) {
                            try {
                                const parsedUser = JSON.parse(rawUser);
                                apiUserId = parsedUser.id ? String(parsedUser.id) : "";
                            } catch (e) {
                                console.error("解析 localStorage 中的 user 失败:", e);
                            }
                        }
    
                        // 2. 构建请求头
                        const headers = {
                            "accept": "application/json, text/plain, */*",
                            "accept-language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
                            "cache-control": "no-store",
                            "pragma": "no-cache",
                            "sec-ch-ua": "\\"Not=A?Brand\\";v=\\"99\\", \\"Google Chrome\\";v=\\"151\\", \\"Chromium\\";v=\\"151\\"",
                            "sec-ch-ua-mobile": "?0",
                            "sec-ch-ua-platform": "\\"Windows\\"",
                            "sec-fetch-dest": "empty",
                            "sec-fetch-mode": "cors",
                            "sec-fetch-site": "same-origin",
                            "sec-gpc": "1"
                        };
    
                        // 如果成功获取到 id，则动态写入 new-api-user
                        if (apiUserId) {
                            headers["new-api-user"] = apiUserId;
                        }
    
                        // 3. 发起请求
                        const response = await fetch("https://agentrouter.org/api/user/self", {
                            headers: headers,
                            referrer: "https://agentrouter.org/console/topup",
                            body: null,
                            method: "GET",
                            mode: "cors",
                            credentials: "include"
                        });
                        
                        if (!response.ok) {
                            return { 
                                success: false, 
                                error: `请求失败，HTTP 状态码: ${response.status}` 
                            };
                        }
                        
                        const resData = await response.json();
                        return { success: true, data: resData, fetchedUserId: apiUserId };
                        
                    } catch (error) {
                        return { success: false, error: error.message };
                    }
                }
            """)
            
            if result_data and result_data.get("success"):
                response_json = result_data.get("data", {})
                user_data = response_json.get("data", {})
                fetched_id = result_data.get("fetchedUserId", "")
                
                # 提取关键信息
                user_id = user_data.get("id", "未知")
                display_name = user_data.get("display_name", "未知")
                github_id = user_data.get("github_id", "未绑定")
                quota = user_data.get("quota", 0)
                used_quota = user_data.get("used_quota", 0)
                
                # 计算美金额度 (quota / 500000)
                quota_usd = round(quota / 500000, 2)
                used_usd = round(used_quota / 500000, 2)
                
                info_text = (
                    f"ID: {user_id} (Header Local ID: {fetched_id}) | "
                    f"Name: {display_name} | "
                    f"GitHub: {github_id} | "
                    f"Quota: ${quota_usd} ({quota}) | "
                    f"Used: ${used_usd}"
                )
                
                self.log(f"✅ 保活成功: {info_text}", "SUCCESS")
                return_msg = f"✅ 保活成功 [{info_text}]\n"
            else:
                err_msg = result_data.get("error") if result_data else "未知错误"
                self.log(f"❌ 保活请求失败: {err_msg}", "WARN")
                return_msg = f"⚠️ 保活请求失败: {err_msg}\n"
                
            time.sleep(2)
            
        except Exception as e:
            self.log(f"保活流程异常: {e}", "WARN")
            return_msg = f"❌ 保活流程异常: {e}\n"
            
        self.shot(page, "完成保活请求")
        return return_msg
    
    def mask_url(self, url):
        url = re.sub(r'code=[^&]+', 'code=***', url)
        url = re.sub(r'state=[^&]+', 'state=***', url)
        return url
    
    def get_storage(self, context):
        """提取 storage_state"""
        try:
            state = context.storage_state()
            self.ag_local = state
            return state
        except Exception as e:
            self.log(f"获取 storage_state 失败: {e}", "WARN")
            return None

    def check_and_process_domain(self, domain):
        if domain.endswith('agentrouter.org/login'):
            return "signin"
        if "callback" in domain:
            return "redirect"
        if domain.endswith('agentrouter.org/console') or domain.endswith('agentrouter.org/dashboard'):
            return "logged"
        return "invalid"
    
    def run(self):
        ok, new_local, msg = False, None, ""
        self.log(f"用户名: {mask_name(self.gh_username)}")
        self.log(f"Session: {'有' if self.gh_session else '无'}")
        self.log(f"登录入口: {BOARD_ENTRY_URL}")
        
        if not self.gh_username:
            self.log("缺少凭据", "ERROR")
            return False, None, "❌ 缺少凭据"
        
        with sync_playwright() as p:
            launch_args = {
                "headless": True,
                "args": [
                    '--no-sandbox',
                    '--disable-blink-features=AutomationControlled',
                    '--disable-infobars',
                    '--exclude-switches=enable-automation',
                ]
            }

            if self.ag_proxy and isinstance(self.ag_proxy, dict):
                p_url = self.ag_proxy
                proxy_config = {
                    "server": f"http://{p_url['server']}:{p_url['port']}",
                    "username": p_url.get('username', ''),
                    "password": p_url.get('password', '')
                } 
                launch_args["proxy"] = proxy_config
                self.log(f"🌐 启用代理配置: {p_url['server']}:{p_url['port']}", "INFO")

            browser = p.chromium.launch(**launch_args)
            
            if self.ag_local:
                context = browser.new_context(
                    storage_state=self.ag_local,
                    viewport={'width': 1920, 'height': 1080},
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'
                )
            else:
                context = browser.new_context(
                    viewport={'width': 1920, 'height': 1080},
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'
                )
            
            page = context.new_page()
            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                window.chrome = { runtime: {} };
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    originalQuery(parameters)
                );
            """)

            try:
                if self.gh_session:
                    try:
                        context.add_cookies([
                            {'name': 'user_session', 'value': self.gh_session, 'domain': 'github.com', 'path': '/'},
                            {'name': 'logged_in', 'value': 'yes', 'domain': 'github.com', 'path': '/'}
                        ])
                        self.log("已加载 Session Cookie", "SUCCESS")
                    except Exception:
                        self.log("加载 Cookie 失败", "WARN")
                        
                self.log("步骤1: 打开 agentrouter 登录页", "STEP")

                for i in range(5):
                    try:
                        page.goto(BOARD_ENTRY_URL, timeout=60000)
                        page.wait_for_load_state('domcontentloaded', timeout=60000)
                        time.sleep(3)
                        
                        resault = self.check_and_process_domain(page.url)
                        self.log(f"页面状态检测: {resault} ({page.url})", "INFO")
                        
                        if resault == "logged":
                            self.log("已有有效登录态，跳过手动点击", "SUCCESS")
                            break
                        
                        if resault == "signin":
                            self.log("步骤2: 点击 GitHub", "STEP")
                            self.shot(page, "准备点击GitHub")
                            
                            if self.click(page, desc="GitHub 登录按钮"):
                                try:
                                    self.log("等待 OAuth 跳转完成...", "INFO")
                                    page.wait_for_url(lambda u: "login" not in u.lower(), timeout=60000)
                                    self.log(f"跳转完成，当前 URL: {self.mask_url(page.url)}", "SUCCESS")
                                    
                                    not_bound_selector = 'p.text-slate-600:has-text("This GitHub account is not bound")'
                                    if page.locator(not_bound_selector).is_visible(timeout=3000):
                                        self.log("❌ 账号未绑定 GitHub！", "ERROR")
                                        shot = self.shot(page, "GitHub未绑定")
                                        if shot:
                                            self.notify.send(title="agentrouter 登录失败", content="账号未绑定 GitHub", image_path=shot)
                                        return False, None, "❌ 账号未绑定 GitHub！"
                                        
                                    break
                                except PlaywrightTimeoutError:
                                    self.log(f"⚠️ 点击响应超时，URL 未改变: {page.url}", "WARN")
                                    continue
                            else:
                                self.log("点击失败，准备下一次重试", "WARN")
                                time.sleep(5)
                                continue
                                
                    except Exception as e:
                        self.log(f"第 {i+1} 次尝试登录异常: {e}", "ERROR")
                        time.sleep(5)

                # 执行保活请求
                renew_msg = self.get_balance_with_token(page)
                
                self.log("步骤3: 更新 local_storage", "STEP")
                storage_state = self.get_storage(context)
                
                if storage_state:
                    self.log("开始为数据瘦身...")
                    slimmest_local = slim_storage_state(storage_state)
                    final_json = json.dumps(slimmest_local, ensure_ascii=False)
                    self.log(f"瘦身完成，最终数据大小: {len(final_json) / 1024:.2f} KB")
                    
                    storage_state_b64 = base64.b64encode(final_json.encode("utf-8")).decode("utf-8")
                    ok = True
                    new_local = storage_state_b64
                    msg = renew_msg
                else:
                    self.log("未获取到 storage_state", "WARN")
                
                if self.shots:
                    self.notify.send(title="agentrouter 自动登录保活", content=f"✅ {self.gh_username} 成功！\n{renew_msg}", image_path=self.shots[-1])
            except Exception as e:
                self.log(f"异常: {e}", "ERROR")
                self.shot(page, "异常")
                import traceback
                traceback.print_exc()
                if self.shots:
                    self.notify.send(title="agentrouter 自动登录保活", content=f"❌ {self.gh_username}:{str(e)}", image_path=self.shots[-1])
                msg = f"访问 {page.url} 失败！"   
            finally:
                if browser:
                    browser.close()
                return ok, new_local, msg

def main():
    global config
    if config is None:
        config = ConfigReader()
    
    results = []
    accounts = config.get_value("GH_INFO")
    proxies = config.get_value("WZ_INFO")

    notify = get_notifier()
    gh_secret = SecretUpdater("GH_SESSION", config_reader=config)
    gh_sessions = gh_secret.load() or {}
    
    secret = SecretUpdater("AGENTROUTER_LOCALS", config_reader=config)
    ag_locals = secret.load() 

    if not accounts:
        print("❌ 错误: 未配置 LEAFLOW_ACCOUNTS")
        return
    if not proxies:
        print("📢 警告: 未配置 proxy ，将直连")

    print(f"📊 检测到 {len(accounts)} 个账号和 {len(proxies)} 个代理")

    for account, proxy in zip(accounts, proxies):
        username = account['username']
        if username == 'you5102':
            continue
        print("\n" + "="*50)
        print(f"\n🚀 开始处理账号: {mask_name(username)}\n  🌐 使用代理: {proxy['server'][:-4]}***\n")
        print("="*50 + "\n")
        
        results.append(f"🚀 账号：{mask_name(username)}\n    🌐 使用代理: {proxy['server'][:-4]}***\n")
        ag_info = {
            'gh_username': username,
            'ag_proxy': proxy,
            'notify': notify,
            'wz_proxy': proxies[-1]
        }

        if isinstance(gh_sessions, dict):
            gh_session = gh_sessions.get(username, '')
            if isinstance(gh_session, list):
                gh_session = gh_session[0] if gh_session else ''
            ag_info['gh_session'] = gh_session
        else:
            print("⚠️ gh_sessions 格式错误！")
            ag_info['gh_session'] = ''

        if not gh_session:
            print("⚠️ 缺少对应账号的 gh_session ，退出！")
            continue
        
        if isinstance(ag_locals, dict):
            ag_local = ag_locals.get(username, '')
            if ag_local:
                try:
                    ag_info['ag_local'] = json.loads(base64.b64decode(ag_local).decode("utf-8"))
                    print("✅ 已加载 storage_state")
                except Exception as e:
                    print(f"❌ 加载 storage_state 失败: {e}")
        else:
            print(f"⚠️ ag_locals 格式错误！{ag_locals}")
            ag_locals = {}
            ag_info['ag_local'] = []

        try:
            auto_login = AutoLogin(ag_info)
            ok, new_local, msg = auto_login.run()
    
            if ok:
                print("    ✅ 执行成功")
                results.append(f"    ✅ {msg}\n")
                if new_local:
                    print("    ✅ 保存新 new_local")
                    ag_locals[username] = new_local
            else:
                print("    ⚠️ 执行失败，不保存 cookie")
                results.append(f"    ⚠️ 执行失败:{msg}\n")
    
        except Exception as e:
            print(f"    ❌ 执行异常: {e}")
            results.append(f"    ❌ 执行异常: {e}")

    print(f"ag_locals数据大小: {len(json.dumps(ag_locals)) / 1024:.2f} KB")
    secret.update(ag_locals)
    notify.send(
        title="agentrouter 自动登录保活汇总",
        content="\n".join(results)
    )

if __name__ == "__main__":
    main()
