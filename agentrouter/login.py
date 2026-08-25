"""
agentrouter.org 自动登录脚本
基于参考代码的 Playwright 模式
"""

import json
import base64
import time
import random
import os
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


class AutoLoginAgentRouter:
    """agentrouter.org 自动登录类"""

    def __init__(self, config):
        self.login_url = "https://agentrouter.org/login"
        self.gh_username = config.get('gh_username', '')
        self.gh_session = config.get('gh_session', '')  # 可选：GitHub Session
        self.dt_local = config.get('dt_local', None)  # 之前保存的 storage_state
        self.dt_proxy = config.get('dt_proxy', None)  # 代理配置
        self.headless = config.get('headless', True)
        self.shots = []
        self.n = 0

    def log(self, msg, level="INFO"):
        """日志输出"""
        icons = {"INFO": "ℹ️", "SUCCESS": "✅", "ERROR": "❌", "WARN": "⚠️", "STEP": "🔹"}
        line = f"{icons.get(level, '•')} {msg}"
        print(line, flush=True)

    def shot(self, page, name):
        """页面截图"""
        self.n += 1
        f = f"{self.n:02d}_{name}.png"
        try:
            page.screenshot(path=f)
            self.shots.append(f)
            self.log(f"已保存截图: {f}", "INFO")
        except:
            pass
        return f

    def find_and_click(self, page, selectors, desc=""):
        """查找并点击元素"""
        self.log(f"🔍 查找: {desc}", "INFO")

        for selector in selectors:
            try:
                el = page.locator(selector).first
                if el.is_visible(timeout=2000):
                    time.sleep(random.uniform(0.5, 1.2))
                    el.hover()
                    time.sleep(random.uniform(0.2, 0.4))
                    el.click(force=True)
                    self.log(f"✅ 已点击: {desc}", "SUCCESS")
                    return True
            except PlaywrightTimeoutError:
                pass
            except Exception as e:
                self.log(f"• 点击失败: {selector}", "INFO")

        self.log(f"❌ 找不到: {desc}", "ERROR")
        return False

    def handle_github_oauth(self, page):
        """处理 GitHub OAuth 流程"""
        self.log("处理 GitHub OAuth...", "STEP")

        try:
            # 等待 GitHub 页面加载
            page.wait_for_url("**github.com**", timeout=30000)
            self.log(f"已跳转到 GitHub: {page.url}", "INFO")
            self.shot(page, "github_oauth_page")

            # 如果已经登录 GitHub，直接授权
            # 如果未登录，需要先登录
            if "login" in page.url:
                self.log("需要登录 GitHub", "WARN")
                # 这里可以添加 GitHub 登录逻辑
                # 但通常这个脚本假设用户已经登录了 GitHub

            # 点击授权按钮
            auth_selectors = [
                'button:has-text("Authorize")',
                'button:has-text("授权")',
                'button[name="authorize"]',
                'input[type="submit"]'
            ]

            for selector in auth_selectors:
                try:
                    el = page.locator(selector).first
                    if el.is_visible(timeout=3000):
                        el.click()
                        self.log("已点击授权按钮", "SUCCESS")
                        time.sleep(3)
                        return True
                except:
                    pass

            self.log("未找到授权按钮，假装已授权", "WARN")
            return True

        except PlaywrightTimeoutError:
            self.log("GitHub OAuth 超时", "ERROR")
            return False
        except Exception as e:
            self.log(f"OAuth 处理失败: {e}", "ERROR")
            return False

    def wait_redirect(self, page, timeout=60):
        """等待重定向回登录完成页面"""
        self.log("等待重定向...", "STEP")

        for i in range(timeout):
            url = page.url

            # 检查是否已返回 agentrouter
            if 'agentrouter.org' in url and 'login' not in url.lower():
                self.log("✅ 重定向成功！", "SUCCESS")
                return True

            # 如果还在 GitHub OAuth 授权页
            if 'github.com/login/oauth/authorize' in url:
                self.log("仍在 OAuth 授权页，尝试点击授权...", "INFO")
                auth_selectors = [
                    'button:has-text("Authorize")',
                    'input[type="submit"]'
                ]
                for selector in auth_selectors:
                    try:
                        el = page.locator(selector).first
                        if el.is_visible(timeout=1000):
                            el.click()
                            time.sleep(3)
                            break
                    except:
                        pass

            time.sleep(1)
            if i % 10 == 0 and i > 0:
                self.log(f"  等待中... ({i}/{timeout}秒) - {url}", "INFO")

        self.log("重定向超时", "ERROR")
        return False

    def run(self):
        """执行自动登录"""
        self.log("="*50, "INFO")
        self.log("agentrouter.org 自动登录", "STEP")
        self.log("="*50, "INFO")

        if not self.gh_username:
            self.log("缺少 GitHub 用户名", "ERROR")
            return False, None

        with sync_playwright() as p:
            # 浏览器启动参数
            launch_args = {
                "headless": self.headless,
                "args": [
                    '--no-sandbox',
                    '--disable-blink-features=AutomationControlled',
                    '--disable-infobars',
                    '--exclude-switches=enable-automation',
                ]
            }

            # 配置代理
            if self.dt_proxy:
                launch_args["proxy"] = {
                    "server": f"http://{self.dt_proxy['server']}:{self.dt_proxy.get('port', 80)}",
                    "username": self.dt_proxy.get('username', ''),
                    "password": self.dt_proxy.get('password', '')
                }
                self.log(f"使用代理: {self.dt_proxy['server']}", "INFO")

            browser = p.chromium.launch(**launch_args)

            try:
                # 创建上下文
                context_args = {
                    "viewport": {'width': 1920, 'height': 1080},
                    "user_agent": 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'
                }

                # 加载之前保存的存储状态（包括 cookies 和 localStorage）
                if self.dt_local:
                    context_args['storage_state'] = self.dt_local
                    self.log("已加载保存的存储状态", "SUCCESS")

                context = browser.new_context(**context_args)

                # 反检测脚本
                page = context.new_page()
                page.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => undefined
                    });
                    Object.defineProperty(navigator, 'plugins', {
                        get: () => [1, 2, 3, 4, 5]
                    });
                    Object.defineProperty(navigator, 'languages', {
                        get: () => ['en-US', 'en']
                    });
                    window.chrome = { runtime: {} };
                """)

                # 加载 GitHub Session（如果有）
                if self.gh_session:
                    try:
                        context.add_cookies([
                            {'name': 'user_session', 'value': self.gh_session, 'domain': 'github.com', 'path': '/'},
                            {'name': 'logged_in', 'value': 'yes', 'domain': 'github.com', 'path': '/'}
                        ])
                        self.log("已加载 GitHub Session", "SUCCESS")
                    except:
                        self.log("加载 GitHub Session 失败", "WARN")

                # 步骤 1: 打开登录页
                self.log("步骤 1: 打开登录页面", "STEP")
                page.goto(self.login_url, timeout=60000)
                page.wait_for_load_state('domcontentloaded', timeout=60000)
                time.sleep(random.uniform(2, 4))
                self.shot(page, "01_登录页")

                # 检查是否已经登录
                current_url = page.url
                if 'login' not in current_url.lower():
                    self.log(f"✅ 已经登录！当前 URL: {current_url}", "SUCCESS")
                    self.shot(page, "02_已登录")

                    # 保存存储状态
                    storage_state = context.storage_state()
                    storage_json = json.dumps(storage_state, ensure_ascii=False)
                    storage_b64 = base64.b64encode(storage_json.encode('utf-8')).decode('utf-8')

                    return True, storage_b64

                # 步骤 2: 查找并点击 GitHub 登录按钮
                self.log("步骤 2: 查找 GitHub 登录按钮", "STEP")
                github_selectors = [
                    'a[href*="/auth/github"]',
                    'a[href*="github"]',
                    'button:has-text("GitHub")',
                    'button:has-text("Sign in with GitHub")',
                    'a:has-text("GitHub")',
                    '[data-provider="github"]',
                    'a.github-login'
                ]

                if not self.find_and_click(page, github_selectors, "GitHub 登录按钮"):
                    self.shot(page, "03_找不到GitHub按钮")
                    return False, None

                time.sleep(random.uniform(3, 5))
                self.shot(page, "04_点击后")

                # 步骤 3: 处理 OAuth 重定向
                self.log("步骤 3: 处理 OAuth 流程", "STEP")

                if not self.wait_redirect(page):
                    self.shot(page, "05_重定向失败")
                    return False, None

                time.sleep(2)
                self.shot(page, "06_登录完成")

                # 步骤 4: 保存新的存储状态
                self.log("步骤 4: 保存存储状态", "STEP")
                storage_state = context.storage_state()

                if storage_state:
                    # 精简存储状态
                    self.log("正在精简数据...", "INFO")

                    if 'cookies' in storage_state:
                        storage_state['cookies'] = [
                            c for c in storage_state['cookies']
                            if 'agentrouter.org' in c.get('domain', '')
                        ]

                    storage_json = json.dumps(storage_state, ensure_ascii=False)
                    storage_b64 = base64.b64encode(storage_json.encode('utf-8')).decode('utf-8')

                    self.log(f"✅ 存储状态已保存，大小: {len(storage_b64) / 1024:.2f} KB", "SUCCESS")

                    return True, storage_b64
                else:
                    self.log("未获取到存储状态", "ERROR")
                    return False, None

            except PlaywrightTimeoutError as e:
                self.log(f"超时: {e}", "ERROR")
                self.shot(page, "error_timeout")
                return False, None
            except Exception as e:
                self.log(f"异常: {e}", "ERROR")
                self.shot(page, "error_exception")
                import traceback
                traceback.print_exc()
                return False, None
            finally:
                browser.close()


def main():
    """主函数"""

    # 配置信息
    config = {
        'gh_username': 'your_github_username',  # 改为你的 GitHub 用户名
        'gh_session': '',  # 可选：之前保存的 GitHub Session
        'dt_local': None,  # 可选：之前保存的存储状态（base64）
        'dt_proxy': None,  # 可选：代理配置 {'server': 'proxy.example.com', 'port': 8080, 'username': '', 'password': ''}
        'headless': False  # 设为 False 可以看到浏览器过程
    }

    # 创建自动登录实例
    auto_login = AutoLoginAgentRouter(config)

    # 执行登录
    success, storage_state_b64 = auto_login.run()

    if success:
        print("\n" + "="*50)
        print("✅ 登录成功！")
        print("="*50)

        # 保存存储状态以备后续使用
        if storage_state_b64:
            print("\n存储状态（用于下次登录）:")
            print(storage_state_b64[:100] + "...")

            # 可以保存到文件或环境变量
            with open('agentrouter_storage_state.txt', 'w') as f:
                f.write(storage_state_b64)
            print("已保存到 agentrouter_storage_state.txt")
    else:
        print("\n" + "="*50)
        print("❌ 登录失败，请检查截图或日志")
        print("="*50)


if __name__ == "__main__":
    main()
