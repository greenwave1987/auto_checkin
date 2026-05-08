import os
import io
import sys
import time
import matplotlib.pyplot as plt
from datetime import datetime, timedelta, timezone
import base64
import json
import socket
import subprocess
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from playwright_stealth import stealth_async

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from engine.notify import TelegramNotifier
from engine.main import ConfigReader, SecretUpdater, test_proxy,to_beijing_time
plt.switch_backend('Agg') # 必须在其他 plt 操作之前执行
LOGIN_URL = "https://freecloud.ltd/login"
DASHBOARD_URL = "https://freecloud.ltd/server/lxc"
BALANCE_URL = "https://freecloud.ltd/balance"
CHECKIN_URL = "https://checkin.freecloud.ltd/"
SCREENSHOT_DIR = "/tmp/freecloud_fail"


# ==================== 工具函数 ====================
def mask_email(email: str):
    if "@" not in email:
        return "***"
    name, domain = email.split("@", 1)
    return f"{name[:2]}***{name[-2:]}@{domain}"

def mask_name(name: str):
    return f"{name[:2]}***{name[-2:]}"


def mask_ip(ip: str):
    return f"***{ip}" if ip else "***"


def mask_password(pwd: str):
    return "*" * 6 + f"({len(pwd)})"


def decode_storage(b64_str):
    try:
        raw = base64.b64decode(b64_str).decode()
        return json.loads(raw)
    except Exception:
        return None


def encode_storage(storage):
    return base64.b64encode(json.dumps(storage).encode()).decode()




# ==================== 核心类 ====================
class freecloudTask:
    def __init__(self):
        self.config = ConfigReader()
        self.logs = []
        self.notifier = TelegramNotifier(self.config)
        self.secret = SecretUpdater("FREECLOUD_LOCALS", config_reader=self.config)
        self.gost_proc = None
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)

    # ---------- 日志 ----------
    def log(self, msg, level="INFO"):
        icons = {"INFO": "ℹ️", "SUCCESS": "✅", "ERROR": "❌", "WARN": "⚠️", "STEP": "🔹"}
        line = f"{icons.get(level,'•')} {msg}"
        print(line, flush=True)
        self.logs.append(line)

 # ---------- Gost ----------
    def start_gost_proxy(self, proxy):
        def free_port():
            s = socket.socket()
            s.bind(("", 0))
            port = s.getsockname()[1]
            s.close()
            return port

        port = free_port()
        server = f"socks5://127.0.0.1:{port}"
        remote = f"socks5://{proxy['username']}:{proxy['password']}@{proxy['server']}:{proxy['port']}"

        self.log(
            f"启动 Gost: ./gost -L :{port} -F ***{proxy['server']}:{proxy['port']}",
            "STEP"
        )

        self.gost_proc = subprocess.Popen(
            ["./gost", "-L", f":{port}", "-F", remote],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        time.sleep(2)
        return {"server": server}

   # ---------- 浏览器 ----------
    def open_browser(self, proxy, storage):
        self.log("启动 Playwright 浏览器", "STEP")
        pw = sync_playwright().start()

        launch_args = {
            "headless": "new",
            "args": [
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--exclude-switches=enable-automation",
            ]
        }

        if proxy:
            if proxy.get("type") == "socks5" and proxy.get("username"):
                gost = self.start_gost_proxy(proxy)
                launch_args["proxy"] = {"server": gost["server"]}
                self.log(f"使用 Gost 本地代理: {gost['server']}", "SUCCESS")
            else:
                server = f"{proxy['type']}://{proxy['server']}:{proxy['port']}"
                launch_args["proxy"] = {"server": server}
                self.log(f"启用代理: {mask_ip(proxy['server'])}", "INFO")

        browser = pw.chromium.launch(**launch_args)
        context = browser.new_context(
            storage_state=storage,
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 Chrome/128.0.0.0"
        )

        page = context.new_page()
        # 应用 stealth 脚本
        await stealth_async(page)
        return pw, browser, page

    # ---------- 截图 ----------
    def capture_and_notify(self, page, user, reason):
        path = f"{SCREENSHOT_DIR}/{user}_{int(time.time())}.png"
        try:
            page.screenshot(path=path, full_page=True, timeout=60000)  # 30秒
        except PlaywrightTimeoutError:
            self.log("截图超时，跳过截图", "WARN")
        self.notifier.send(
            
            title=f"ℹ️ FreeCloud 登录\n",content=f"账号: {mask_name(user)}\n原因: {reason}",image_path=path
        )



    # ---------- 登录 ----------
    def do_login(self, page, user, pwd):
        self.log(f"打开登录页: {LOGIN_URL}", "STEP")
        # 建议使用 wait_until="networkidle" 确保验证码加载出来
        page.goto(LOGIN_URL, wait_until="networkidle")
    
        # 1. 输入账号 (使用你提供的 name="username")
        self.log(f"输入账号: {mask_email(user)}", "INFO")
        page.locator('input[name="username"]').fill(user)
    
        # 2. 输入密码 (使用 name="password")
        self.log(f"输入密码: {mask_password(pwd)}", "INFO")
        page.locator('input[name="password"]').fill(pwd)
    
        # 3. 处理数学验证码 (核心修改)
        try:
            self.log("正在识别数学验证码...", "STEP")
            # 验证码输入框通常带有 placeholder="X + Y = ?"
            captcha_input = page.locator('input[placeholder*="="]')
            captcha_text = captcha_input.get_attribute("placeholder")
            
            # 提取数字并计算
            nums = re.findall(r'\d+', captcha_text)
            if len(nums) >= 2:
                result = str(int(nums[0]) + int(nums[1]))
                captcha_input.fill(result)
                self.log(f"验证码计算成功: {nums[0]} + {nums[1]} = {result}", "INFO")
            else:
                self.log("无法解析验证码文本", "ERROR")
        except Exception as e:
            self.log(f"验证码处理异常: {e}", "WARN")
    
        # 4. 勾选协议 (截图显示这是登录的前提)
        try:
            self.log("勾选法律声明协议", "STEP")
            # 页面只有一个 checkbox
            page.locator('input[type="checkbox"]').check()
        except Exception:
            self.log("未找到协议复选框", "WARN")
    
        # 5. 点击登录按钮
        self.log("点击登录按钮", "STEP")
        # 页面显示按钮文本为 "点击登录"
        page.locator('button:has-text("点击登录")').click()
        
        # 增加等待时间，防止 GitHub 这种慢速环境跳转过快
        try:
            page.wait_for_url(re.compile(r".*/index|.*/dashboard"), timeout=30000)
        except Exception:
            pass
    
        if "login" in page.url.lower():
            # 如果还在登录页，尝试截个图方便调试
            page.screenshot(path="login_failed.png")
            raise RuntimeError("登录失败：页面未跳转，请检查账号或验证码")
    
        self.log("登录成功", "SUCCESS")

    # ---------- 验证 storage ----------
    def ensure_login(self, page, user, pwd, max_retry=5):
        """
        验证 storage 是否有效
        失败自动重试
        返回:
            True  -> 执行了重新登录
            False -> storage 有效
        """
    
        for attempt in range(1, max_retry + 1):
            try:
                self.log(f"验证登录状态 (第 {attempt}/{max_retry} 次)", "INFO")
    
                page.goto(DASHBOARD_URL, timeout=60000)
                page.wait_for_load_state("domcontentloaded", timeout=60000)
    
                # 给页面一点时间跳转
                page.wait_for_timeout(120000)
    
                current_url = page.url.lower()
    
                if "login" in current_url:
                    self.log("storage 已失效，重新登录", "WARN")
                    self.do_login(page, user, pwd)
                    return True

    
                self.log("storage 有效，跳过登录", "SUCCESS")
                return False
    
            except Exception as e:
                self.log(f"验证异常: {str(e)}", "ERROR")
    
                if attempt < max_retry:
                    self.log("等待后重试...", "WARN")
                    page.wait_for_timeout(3000)
                    continue
                else:
                    self.log("多次失败，强制重新登录", "ERROR")
                    self.do_login(page, user, pwd)
                    return True
    
        # ---------- 获取金额信息 ----------
    def get_balance_data(self, page, max_retry=5):
        """
        通过 API 获取账户余额信息
        自动重试 + 状态校验
        """
    
        api_script = """
        async () => {
            try {
                const response = await fetch("https://freecloud.ltd/balance", {
                    headers: {
                        "x-inertia": "true",
                        "x-inertia-version": "1da8f358bacd543adbf104c91fa91267",
                        "x-requested-with": "XMLHttpRequest"
                    },
                    method: "GET"
                });
    
                return {
                    status: response.status,
                    ok: response.ok,
                    data: response.ok ? await response.json() : null
                };
    
            } catch (err) {
                return {
                    status: -1,
                    ok: false,
                    error: err.toString()
                };
            }
        }
        """
    
        for attempt in range(1, max_retry + 1):
            try:
                self.log(f"获取余额信息 (第 {attempt}/{max_retry} 次)", "STEP")
    
                page.goto(DASHBOARD_URL, timeout=60000)
                page.wait_for_load_state("domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)
    
                result = page.evaluate(api_script)
    
                # JS执行异常
                if result is None:
                    raise Exception("返回数据为空")
    
                # 网络错误
                if result.get("status") == -1:
                    raise Exception(result.get("error"))
    
                # 未登录
                if result.get("status") in [401, 403]:
                    self.log("检测到登录失效", "WARN")
                    return "LOGIN_EXPIRED"
    
                # 成功
                if result.get("ok"):
                    self.log("余额信息获取成功", "SUCCESS")
                    return result.get("data")
    
                # 其他异常状态
                raise Exception(f"HTTP {result.get('status')}")
    
            except Exception as e:
                self.log(f"获取失败: {str(e)}", "WARN")
    
                if attempt < max_retry:
                    self.log("等待后重试...", "INFO")
                    page.wait_for_timeout(3000)
                    continue
                else:
                    self.log("多次失败，放弃获取余额", "ERROR")
                    return None

    # ---------- 签到 ----------
    def get_checkin_info(self, page):
        # 1. 先通过 API 获取数据
        raw_info = self.get_balance_data(page)
        if raw_info:
            report = self.process_freecloud_api(raw_info)
            self.user=report['username']
            if report['is_checked_today']:
                
                self.log(f"今日已签到 (用户: {mask_name(report['username'])}, 余额: {report['balance']})", "SUCCESS")
                
                status_emoji = "✅" if report["is_checked_today"] else "❌"
                msg = (
                    f"📊 **freecloud 资产报告**\n"
                    f"👤 用户: {mask_name(report['username'])}\n"
                    f"💰 余额: {report['balance']}\n"
                    f"📉 已用: {report['consumed']}\n"
                    f"🕒 签到: {report['last_checkin_time']}\n"
                    f"💴 奖励: {report['last_checkin_amount']}\n"
                    f"📅 今日: {status_emoji}"
                )
                
                # --- 修复 BytesIO 发送问题 ---
                if report["chart_buf"]:
                    # 定义临时路径
                    temp_chart_path = f"{SCREENSHOT_DIR}/chart_{report['username']}.png"
                    try:
                        # 将 BytesIO 写入本地文件
                        with open(temp_chart_path, "wb") as f:
                            f.write(report["chart_buf"].getbuffer())
                        
                        # 调用原有的发送方法（传入路径字符串）
                        self.notifier.send(
                            title=f"freecloud 签到报告",
                            content=msg,
                            image_path=temp_chart_path
                        )
                        # 发送后清理临时文件
                        if os.path.exists(temp_chart_path):
                            os.remove(temp_chart_path)
                    except Exception as e:
                        self.log(f"图片保存或发送失败: {e}", "WARN")
                        self.notifier.send(title="freecloud 签到报告", content=msg)
                else:
                    self.notifier.send(title="freecloud 签到报告", content=msg)
                #已签到，返回True
                return True
            else:
                self.log(f"今日还未签到!", "WARN")


    # ---------- 签到 (终极稳健版) ----------
    def do_checkin(self, page):
        if self.get_checkin_info(page):
            return
              
        self.log("API 显示未签到，准备执行点击签到...", "STEP")
        
        checkin_btn_selector = 'button.checkin-btn'
        success_text_selector = 'div.mt-2.mb-1.text-muted.small:has-text("今日已签到")'
        
        for attempt in range(15):
            try:
                self.log(f"第 {attempt+1} 次尝试访问签到页: {CHECKIN_URL}", "STEP")
                
                # 优化点 1: 使用 domcontentloaded 减少因加载某个图片/广告导致的 90s 超时
                page.goto(CHECKIN_URL, wait_until="domcontentloaded", timeout=60000)
                
                # 检查是否已签到（防止 API 缓存导致的误判）
                # 等待 5 秒给 JS 执行时间
                time.sleep(15)
                if page.locator(success_text_selector).count() > 0:
                    self.log("页面检测到今日已签到", "SUCCESS")
                    if page:
                        self.capture_and_notify(page, self.user, "今日已签到!")
                    
                    self.get_checkin_info(page)
                    return

                # 优化点 2: 显式等待按钮可见
                self.log("等待签到按钮出现...", "INFO")
                btn = page.wait_for_selector(checkin_btn_selector, state="visible", timeout=60000)
                
                if btn:
                    self.log("发现签到按钮，执行点击", "SUCCESS")
                    # 优化点 3: 增加点击前的小延迟，模拟真人操作
                    time.sleep(5)
                    btn.click()
                    
                    # 优化点 4: 循环检查签到状态（最多等 15 秒）
                    for _ in range(15):
                        if page.locator(success_text_selector).count() > 0:
                            self.log("签到确认成功", "SUCCESS")
                            break
                        time.sleep(15)
                    
                    if page:
                        self.capture_and_notify(page, self.user, "签到状态!")
                    if self.get_checkin_info(page):
                        return
                    raise RuntimeError("点击了按钮但状态未更新")

            except Exception as e:
                self.log(f"第 {attempt+1} 次尝试异常: {str(e)}", "WARN")
                if attempt < 14:
                    # 失败后刷新页面或等待重试
                    time.sleep(10)
                else:
                    self.capture_and_notify(page, self.user, f"签到最终失败: {str(e)}")
                    raise RuntimeError("签到流程重试耗尽")

    # ---------- 签到 ----------  
    def jdo_checkin(self, page):
        # 1. 先通过 API 获取数据判断是否签到
        if self.get_checkin_info(page):
            return
              
        # 2. 如果未签到，执行点击逻辑...
        self.log("API 显示未签到，准备执行点击签到...", "STEP")
        
        for attempt in range(15):
            try:
                self.log(f"第 {attempt+1} 次打开签到页: {CHECKIN_URL}", "STEP")
                page.goto(CHECKIN_URL, wait_until="domcontentloaded", timeout=120000)
                time.sleep(20)
                break
            except PlaywrightTimeoutError:
                self.log(f"第 {attempt+1} 次访问签到页失败，重试中...", "WARN")
                time.sleep(2)
        else:
            if page:
                self.capture_and_notify(page, self.user, "访问签到页失败")
            raise RuntimeError("访问签到页失败")
    
        # 先检查是否已经签到
        checked_div = page.locator('div.mt-2.mb-1.text-muted.small', has_text="今日已签到")
        if checked_div.count() > 0:
            self.log("今日已签到，跳过点击", "SUCCESS")
            if page:
                self.capture_and_notify(page, self.user, "今日已签到!")
            return
    
        # 查找立即签到按钮
        time.sleep(20)
        btn = page.locator('button.checkin-btn')
        if btn.count() == 0:
            self.log("未发现签到按钮，可能页面未完全加载或已签到", "WARN")
            if page:
                self.capture_and_notify(page, self.user, "未发现签到按钮，可能页面未完全加载或已签到!")
            return
    
        # 点击签到
        self.log("点击「立即签到」按钮", "STEP")
        try:
            btn.first.click(timeout=60000)
            time.sleep(2)
    
            # 点击后再次确认是否签到成功
            checked_div = page.locator('div.mt-2.mb-1.text-muted.small', has_text="今日已签到")
            if checked_div.count() > 0:
                self.log("签到成功", "SUCCESS")
            else:
                self.log("点击签到按钮后未检测到签到状态", "WARN")
                if page:
                    self.capture_and_notify(page, self.user, "点击签到按钮后未检测到签到状态")
    
        except PlaywrightTimeoutError:
            self.log("点击签到按钮超时，可能页面未完全渲染", "WARN")
            if page:
                self.capture_and_notify(page, self.user, "点击签到按钮超时，可能页面未完全渲染")
    # ---------- 数据处理与图表生成 ----------
    def process_freecloud_api(self, json_data):
        """
        解析 freecloud API 数据并生成统计报表
        """
        # 1. 安全提取各级数据，防止 KeyError
        props = json_data.get("props", {})
        user_info = props.get("auth", {}).get("user", {})
        records = props.get("records", {}).get("data", [])

        # 2. 初始化结果结构f"{props.get("totalConsumed", "0.00"):.2f}"
        res = {
            "username": user_info.get("name", "Unknown"),
            "balance": f'{float(props.get("balance", 0)):.2f}',
            "consumed": f'{float(props.get("totalConsumed", 0)):.2f}',
            "last_checkin_time": "无记录",
            "last_checkin_amount": "无记录",
            "is_checked_today": False,
            "daily_history": {},  # 用于绘图
            "chart_buf": None     # 图片流
        }

        # 3. 处理签到记录
        now_bj = datetime.now(timezone(timedelta(hours=8)))
        today_str = now_bj.strftime("%Y-%m-%d")

        if records:
            # 记录最后一次签到时间
            last_dt = to_beijing_time(records[0].get("created_at"))
            if last_dt:
                res["last_checkin_time"] = last_dt.strftime("%Y-%m-%d %H:%M:%S")

            # 遍历历史记录进行统计 (按北京时间)
            for r in reversed(records):
                remark = r.get("remark", "")
                if "奖励" in remark or "签到" in remark:
                    bj_dt = to_beijing_time(r.get("created_at"))
                    if bj_dt:
                        date_key = bj_dt.strftime("%Y-%m-%d")
                        amount = float(r.get("amount", 0))
                        
  
                        # 汇总每天的金额
                        res["daily_history"][date_key] = res["daily_history"].get(date_key, 0) + amount
                        
                        # 判定今日是否已签到
                        if date_key == today_str:
                            res["is_checked_today"] = True
            res["last_checkin_amount"] = f'{ float(res["daily_history"].get(today_str, 0)):.2f}'
            
        # 4. 绘图逻辑
        if res["daily_history"]:
            plt.figure(figsize=(10, 5))
            # 仅取最近12天日期展示
            dates = list(res["daily_history"].keys())[-12:]
            amounts = [res["daily_history"][d] for d in dates]

            plt.plot(dates, amounts, marker='o', color='#10a37f', linewidth=2)
            plt.fill_between(dates, amounts, color='#10a37f', alpha=0.1)
            plt.title(f"Check-in Rewards: {mask_name(res['username'])}", fontsize=12)
            plt.xticks(rotation=30)
            plt.grid(True, linestyle=':', alpha=0.6)
            plt.tight_layout()

            buf = io.BytesIO()
            plt.savefig(buf, format='png')
            buf.seek(0)
            plt.close()
            res["chart_buf"] = buf

        return res

    # ---------- 主流程 ----------
    def run(self):
        self.log("freecloud 多账号任务启动", "STEP")

        accounts = self.config.get_value("FC_INFO") or []
        proxies = self.config.get_value("WZ_INFO") or []
        lf_locals = self.secret.load() or {}

        new_sessions = {}

        for account, proxy in zip(accounts, proxies):
            try:
                print("\n" + "="*50)
                user = account["username"]
                pwd = account["password"]

                #proxy=proxies[-1]
    
                self.log(f"开始处理账号: {mask_email(user)}", "STEP")
                self.log(f"检测代理: {mask_ip(proxy['server'])}", "STEP")
                test_proxy(proxy)
    
                storage = None
                if user in lf_locals:
                    storage = decode_storage(lf_locals[user])
    
                pw = browser = None
                try:
                    pw, browser, page = self.open_browser(proxy, storage)
    
                    refreshed = self.ensure_login(page, user, pwd)
                    self.do_checkin(page)
    
                    if refreshed or not storage:
                        self.log("更新 storage", "STEP")
                        new_sessions[user] = page.context.storage_state()
    
                except Exception as e:
                    self.log(f"{mask_email(user)} 登录异常: {e}", "ERROR")
                    if page:
                        self.capture_and_notify(page, user, str(e))
    
                finally:
                    if browser:
                        try:
                            browser.close()
                        except Exception:
                            pass
                    if pw:
                        try:
                            pw.stop()
                        except Exception:
                            pass
                    if self.gost_proc:
                        self.gost_proc.terminate()
                        self.gost_proc = None
            except Exception as e:
                self.log(f"处理账号 {user} 时发生未预期错误: {e}", "ERROR")
                # 可以在这里增加一层保护，防止 notifier 本身报错导致崩溃
                try:
                    self.capture_and_notify(page, user, str(e))
                except:
                    pass
            #break
            
        if new_sessions:
            self.log("准备回写 GitHub Secret", "STEP")
            encoded = {k: encode_storage(v) for k, v in new_sessions.items()}
            self.secret.update(encoded)
            self.log("Secret 回写成功", "SUCCESS")

        self.log("开始发送通知", "STEP")
        #self.notifier.send(title="freecloud 自动签到结果", content="\n".join(self.logs))


if __name__ == "__main__":
    freecloudTask().run()
