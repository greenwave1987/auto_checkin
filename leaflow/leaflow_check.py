import os
import json
import time
import tempfile
import subprocess
from playwright.sync_api import sync_playwright


class LeaflowCheck:

    def __init__(self, tg_bots, lf_info, proxy_info, lf_locals):
        self.tg_bots = tg_bots
        self.lf_info = lf_info
        self.proxy_info = proxy_info
        self.lf_locals = lf_locals

    def log(self, msg, level="INFO"):
        print(f"[{level}] {msg}", flush=True)

    # ===============================
    # 🔧 FIX 1：storageState 写入文件
    # ===============================
    def dump_storage_state(self, storage_state):
        if not storage_state:
            return None

        fd, path = tempfile.mkstemp(prefix="pw_state_", suffix=".json")
        os.close(fd)

        with open(path, "w", encoding="utf-8") as f:
            json.dump(storage_state, f, ensure_ascii=False)

        return path

    def start_gost_proxy(self, p_url):
        port = 40000 + int(time.time()) % 20000
        proxy = f"{p_url['server']}:{p_url['port']}"

        cmd = [
            "./gost",
            "-L", f":{port}",
            "-F", f"socks5://{p_url['username']}:{p_url['password']}@{proxy}"
        ]

        self.log(f"启动 Gost: {' '.join(cmd)}")
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        time.sleep(3)

        return {
            "server": f"http://127.0.0.1:{port}"
        }

    def open_browser(self, proxy, storage_state):
        pw = sync_playwright().start()

        launch_args = {
            "headless": True
        }

        if proxy:
            launch_args["proxy"] = proxy

        browser = pw.chromium.launch(**launch_args)

        # 🔧 FIX：storageState 只能传文件路径
        state_file = self.dump_storage_state(storage_state)

        context_args = {
            "viewport": {"width": 1280, "height": 800},
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }

        if state_file:
            context_args["storage_state"] = state_file

        context = browser.new_context(**context_args)
        page = context.new_page()

        return pw, browser, page, state_file

    # ===============================
    # 🔧 FIX 2：登录失败截图并发 TG
    # ===============================
    def send_login_fail(self, page, user):
        ts = time.strftime("%Y%m%d_%H%M%S")
        img = f"/tmp/leaflow_login_fail_{user}_{ts}.png"

        page.screenshot(path=img, full_page=True)
        self.log(f"🧪 登录失败截图已保存: {img}", "ERROR")

        for bot in self.tg_bots:
            bot.send_photo(
                photo_path=img,
                caption=f"❌ Leaflow 登录失败\n账号: {user}"
            )

    def run(self):
        self.log("🔹 Leaflow 多账号任务启动")

        for idx, user in enumerate(self.lf_info):
            self.log(f"🔹 开始处理账号: {user}")

            proxy = None

            if self.proxy_info:
                p_url = self.proxy_info[idx % len(self.proxy_info)]
                self.log(f"🔹 检测代理: ***{p_url['server']}")

                try:
                    if (
                        p_url.get("type") == "socks5"
                        and p_url.get("username")
                        and p_url.get("password")
                    ):
                        gost = self.start_gost_proxy(p_url)
                        proxy = {"server": gost["server"]}
                        self.log(f"使用 Gost 本地代理: {gost['server']}", "SUCCESS")
                    else:
                        proxy = {
                            "server": f"{p_url['type']}://{p_url['server']}:{p_url['port']}"
                        }
                        self.log(f"启用代理: {proxy['server']}")
                except Exception as e:
                    self.log(f"代理配置解析失败: {e}", "ERROR")
                    proxy = None

            pw = browser = page = state_file = None

            try:
                pw, browser, page, state_file = self.open_browser(
                    proxy,
                    self.lf_locals.get(user)
                )

                page.goto("https://leaflow.net", timeout=60_000)
                page.wait_for_load_state("networkidle", timeout=60_000)

                if "login" in page.url:
                    raise RuntimeError("Cookie 已失效，跳转登录页")

                self.log(f"✅ {user} 登录成功", "SUCCESS")

            except Exception as e:
                self.log(f"❌ {user} 登录异常: {e}", "ERROR")

                if page:
                    self.send_login_fail(page, user)

            finally:
                try:
                    if browser:
                        browser.close()
                    if pw:
                        pw.stop()
                    if state_file and os.path.exists(state_file):
                        os.remove(state_file)
                except Exception:
                    pass
