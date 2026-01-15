# engine/notify.py
# -*- coding: utf-8 -*-

import os
import requests
from engine.safe_print import desensitize_text
from engine.main import ConfigReader


class TelegramNotifier:
    def __init__(self, config: ConfigReader, default_index: int = 0):
        """
        :param default_index: 默认使用第几个 TG Bot（0=第一个）
        """
        self.config = config
        self.session = requests.Session()

        self.bots = self._load_all_bots()
        self.current_index = default_index

        self._apply_bot(self.current_index)

    # =========================
    # 配置读取
    # =========================

    def _load_all_bots(self) -> list[dict]:
        tg_info = self.config.get_value("TG_BOT") or []
    
        if not isinstance(tg_info, list) or not tg_info:
            raise RuntimeError("❌ TG_BOT 配置为空或格式错误")
    
        print(f"✅ 已加载 {len(tg_info)} 个 Telegram Bot")
        return tg_info

    def _apply_bot(self, index: int):
        bot = self.bots[index]
        self.token = bot.get("token")
        self.chat_id = bot.get("id")

        if not self.token or not self.chat_id:
            raise RuntimeError(f"❌ TG_BOT[{index}] token / id 缺失")

        print(f"🤖 当前使用 Telegram Bot[{index}]")

    # =========================
    # 自动降级
    # =========================

    def _switch_bot(self) -> bool:
        if self.current_index + 1 >= len(self.bots):
            print("❌ 已无可用的 Telegram Bot 可切换")
            return False

        self.current_index += 1
        self._apply_bot(self.current_index)

        print(f"🔁 已切换到 Telegram Bot[{self.current_index}]")
        return True

    # =========================
    # 内部发送封装
    # =========================

    def _send_text_once(self, text: str) -> bool:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        r = self.session.post(url, data=payload, timeout=30)
        print(f"⬅️ [TG] HTTP {r.status_code}")
        if not r.ok:
            print(f"❌ [TG] 失败响应: {r.text}")
        return r.ok

    def _send_image_once(self, image_path: str, caption: str | None) -> bool:
        url = f"https://api.telegram.org/bot{self.token}/sendPhoto"
        data = {"chat_id": self.chat_id}

        if caption:
            data["caption"] = caption

        with open(image_path, "rb") as f:
            files = {"photo": f}
            r = self.session.post(url, data=data, files=files, timeout=60)

        print(f"⬅️ [TG] HTTP {r.status_code}")
        if not r.ok:
            print(f"❌ [TG] 失败响应: {r.text}")
        return r.ok

    # =========================
    # 对外接口
    # =========================

    def send(self, title: str, content: str, image_path: str | None = None) -> bool:
        print("🔔 开始发送通知")

        message = f"<b>{title}</b>\n\n{content}"
        message = desensitize_text(message)

        # -------- 文字 --------
        try:
            ok = self._send_text_once(message)
        except Exception as e:
            print(f"💥 TG 文字发送异常: {e}")
            ok = False

        if not ok and self._switch_bot():
            print("🔁 重试发送文字")
            ok = self._send_text_once(message)

        # -------- 图片 --------
        ok_img = True
        if image_path and os.path.exists(image_path):
            try:
                ok_img = self._send_image_once(
                    image_path,
                    caption=desensitize_text(title),
                )
            except Exception as e:
                print(f"💥 TG 图片发送异常: {e}")
                ok_img = False

            if not ok_img and self._switch_bot():
                print("🔁 重试发送图片")
                ok_img = self._send_image_once(
                    image_path,
                    caption=desensitize_text(title),
                )

        return ok and ok_img
