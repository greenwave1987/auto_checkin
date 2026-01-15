# engine/notify.py
# -*- coding: utf-8 -*-

import os
import requests
from engine.safe_print import desensitize_text
from engine.config_reader import ConfigReader


class TelegramNotifier:
    def __init__(self, config: ConfigReader, bot_index: int = 0):
        """
        :param config: ConfigReader 实例
        :param bot_index: 使用第几个 TG bot（0 / 1）
        """
        self.bot_index = bot_index
        self.token = None
        self.chat_id = None
        self.session = requests.Session()

        self._load_from_config(config)

    # =========================
    # 配置读取
    # =========================

    def _load_from_config(self, config: ConfigReader):
        tg_info = config.get("TG_BOT", {}).get("value", [])

        if not tg_info:
            raise RuntimeError("❌ TG_BOT 配置为空")

        if self.bot_index >= len(tg_info):
            raise IndexError(f"❌ TG_BOT index={self.bot_index} 越界")

        bot = tg_info[self.bot_index]

        self.token = bot.get("token")
        self.chat_id = bot.get("id")

        if not self.token or not self.chat_id:
            raise RuntimeError("❌ TG_BOT token / id 缺失")

        print(f"✅ Telegram Bot[{self.bot_index}] 已加载")

    # =========================
    # 内部检查
    # =========================

    def _check(self):
        if not self.token:
            print("❌ TG token 未设置")
            return False
        if not self.chat_id:
            print("❌ TG chat_id 未设置")
            return False
        return True

    # =========================
    # 文本通知
    # =========================

    def send_text(self, text: str) -> bool:
        if not self._check():
            return False

        print("📨 [TG] 发送文字通知")

        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        try:
            r = self.session.post(url, data=payload, timeout=30)
            print(f"⬅️ [TG] HTTP {r.status_code}")
            if not r.ok:
                print(f"❌ [TG] 失败响应: {r.text}")
            return r.ok
        except Exception as e:
            print(f"💥 [TG] 异常: {e}")
            return False

    # =========================
    # 图片通知
    # =========================

    def send_image(self, image_path: str, caption: str | None = None) -> bool:
        if not self._check():
            return False

        if not os.path.exists(image_path):
            print("❌ 图片文件不存在")
            return False

        print(f"🖼️ [TG] 发送图片: {image_path}")

        url = f"https://api.telegram.org/bot{self.token}/sendPhoto"
        data = {"chat_id": self.chat_id}

        if caption:
            data["caption"] = caption

        try:
            with open(image_path, "rb") as f:
                files = {"photo": f}
                r = self.session.post(url, data=data, files=files, timeout=60)

            print(f"⬅️ [TG] HTTP {r.status_code}")
            if not r.ok:
                print(f"❌ [TG] 失败响应: {r.text}")
            return r.ok
        except Exception as e:
            print(f"💥 [TG] 异常: {e}")
            return False

    # =========================
    # 统一入口（推荐）
    # =========================

    def send(self, title: str, content: str, image_path: str | None = None) -> bool:
        print("🔔 开始发送通知")

        message = f"<b>{title}</b>\n\n{content}"
        message = desensitize_text(message)

        ok_text = self.send_text(message)

        ok_img = True
        if image_path:
            ok_img = self.send_image(
                image_path,
                caption=desensitize_text(title),
            )

        return ok_text and ok_img
