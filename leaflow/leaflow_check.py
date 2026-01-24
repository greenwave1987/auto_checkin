import os
import json
import time
import base64
import tempfile
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError


class LeaflowCheck:
    def __init__(self, config, logger, tg_notify):
        self.config = config
        self.log = logger
        self.tg_notify = tg_notify

        self.lf_proxy = (
            config.get("lf_proxy", "").strip()
            if isinstance(config.get("lf_proxy", ""), str)
            else config.get("lf_proxy")
        )

    # =========================
    # storage 工具函数（新增）
    # =========================
    def decode_storage(self, storage_b64: str):
        try:
            raw = base64.b64decode(storage_b64).decode("utf-8")
            return json.loads(raw)
        except Exception as e:
            self.log(f"storage 解码失败，视为过期: {e}", "WARNING")
            return None

    def encode_storage(self, storage_json: dict):
        raw = json.dumps(storage_json, ensure_ascii=False)
        return base64.b64encode(raw.encode("utf-8")).decode("utf-8")

    # =========================
    # 打开浏览器（只改 storage）
    # =========================
    def open_browser(self, proxy, storage_b64):
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)

        context_args = {}

        if proxy:
            context_args["proxy"] = proxy

        if storage_b64:
            storage_json = self.decode_storage(storage_b64)
            if storage_json:
                context_args["storageState"] = storage_json

        context = browser.new_context(**context_args)
        page = context.new_page()
        return pw, browser, context, page

    # =========================
    # 登录流程（你原来就有）
    # =========================
    def do_login(self, page, email, password):
        self.log("🔐 执行登录流程")
        page.goto("https://checkin.leaflow.net/login", timeout=60000)

        page.fill('input[name="email"]', email)
        page.fill('input[name="password"]', password)
        page.click('button[type="submit"]')

        page.wait_for_load_state("networkidle", timeout=60000)

        if "/login" in page.url:
            raise Exception("登录失败，仍停留在登录页")

    # =========================
    # 签到流程（新增）
    # =========================
    def do_checkin(self, page):
        self.log("🔹 打开签到页面")
        page.goto("https://checkin.leaflow.net/", timeout=60000)

        btn = page.wait_for_selector(
            'button.checkin-btn[name="checkin"]',
            timeout=60000
        )

        btn.click()
        self.log("✅ 已点击立即签到")

    # =========================
    # 主执行逻辑
    # =========================
    def run(self, lf_users, lf_locals, proxy_list):
        self.log("🔹 Leaflow 多账号任务启动")

        for (user, pwd), proxy in zip(lf_users, proxy_list):
            self.log(f"🔹 开始处理账号: {user}")

            try:
                pw, browser, context, page = self.open_browser(
                    proxy,
                    lf_locals.get(user)
                )

                page.goto("https://checkin.leaflow.net/", timeout=60000)
                page.wait_for_load_state("networkidle")

                # === 判断是否被踢回登录页 ===
                if "/login" in page.url:
                    self.log("🔁 storage 失效，重新登录")
                    self.do_login(page, user, pwd)

                # === 登录 / 验证成功后更新 storage ===
                storage_state = context.storage_state()
                lf_locals[user] = self.encode_storage(storage_state)
                self.log("💾 storage 已更新")

                # === 执行签到 ===
                self.do_checkin(page)

                self.log(f"🎉 {user} 签到完成", "SUCCESS")

            except Exception as e:
                self.log(f"❌ {user} 登录异常: {e}", "ERROR")
                self.tg_notify(f"❌ Leaflow 登录失败\n账号：{user}\n错误：{e}")

            finally:
                try:
                    browser.close()
                    pw.stop()
                except Exception:
                    pass
